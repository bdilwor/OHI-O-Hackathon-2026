"""Check every number in the pitch quote against the challenge data, with uncertainty.

    docker compose up -d db loader                                  # loaded database (+ asset_risk)
    docker compose run --rm ml python -m ml.build_dataset            # parquet inputs (~2 min)
    docker compose run --rm ml python -m analysis.quote_check        # this script (~15 min)
    docker compose run --rm ml python -m analysis.quote_check --quick   # fewer bootstrap reps / homes

Writes analysis/results/quote_check.json and prints a report. Findings are written up in
analysis/QUOTE_CHECK.md.

The quote:
  (1) "The obvious approach, assuming last storm's outages repeat, warned just 3.6% of homes that lost power."
  (2) "Just using the grid's structure beats that by half with the same number of warnings."
  (3) "On storms it never saw, the riskiest 5% of equipment held a third of the damage."
  (4) "It tells you when it's coming back and how sure we are."

Labels
  ARCOS records equipment damage, not outages. A home's outage label is derived exactly as in
  lookup_vs_no.py: each damage record is snapped to the nearest fuse / recloser / transformer, and a
  home is "out" in a storm if any device on its supply path (premise_upline) was hit. A sensitivity
  variant drops records more than 60 m from any device.

Methods compared at the home level (same number of warnings as the lookup)
  lookup            out in the previous storm (three readings of "last storm" are tested)
  path length       number of devices on the home's supply path (grid structure, no history, no training)
  structure model   the project's logistic regression without any damage-history feature
  history model     the same regression with only damage-history features (+ storm size)
  full model        the project's model (all features), sigmoid-calibrated
  Model scores are rolled up to homes on the same definition as the labels: each asset is assigned to
  its nearest device, P(device hit) = 1 - prod(1 - p_asset), P(home out) = 1 - prod over path devices.
  "Never saw" = leave-one-storm-out training (as in METRICS.md); "past only" = trained only on storms
  before the scored storm (a stricter, forecast-like test).

Uncertainty
  Outages are strongly clustered (one recloser takes out thousands of homes), so confidence intervals
  come from a cluster bootstrap that resamples whole circuits, not homes or assets.
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from scipy.stats import spearmanr
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, mean_pinball_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from ml import duration as D
from ml import features as F
from ml.db import ARTIFACTS, get_engine, read_frame
from ml.train import EVAL_NEG_RATE_MINOR, group_splits, topk_recall

OUT = Path(__file__).resolve().parent / "results"
CACHE = Path(__file__).resolve().parent / ".cache"
SNAP_DEVICE_TYPES = ("fuse", "recloser", "transformer")  # same device set as lookup_vs_no.py
SNAP_CAP_M = 60  # sensitivity label: ignore damage records farther than this from any device
TOP_FRAC = 0.05
BIG_STORM = 100  # reports; Helene (194) and Feb 2025 (1254) are the only storms this large
SEED = 20261025

STRUCTURE = [f for f in F.NUMERIC if f not in F.HISTORY]  # includes storm size, constant within a storm
HISTORY_ONLY = F.HISTORY + ["storm_log_damage"]
FEATURE_SETS = {
    "full": (F.NUMERIC, F.CATEGORICAL),
    "structure": (STRUCTURE, F.CATEGORICAL),
    "history": (HISTORY_ONLY, []),
}


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------

def soft_top(score, k: int) -> np.ndarray:
    """Selection weights for the top-k scores, splitting ties at the cut evenly (= random tie-break)."""
    s = np.asarray(score, dtype=float)
    k = int(min(max(k, 0), len(s)))
    if k == 0:
        return np.zeros(len(s))
    t = np.partition(s, len(s) - k)[len(s) - k]  # k-th largest
    above, tie = s > t, s == t
    sel = above.astype(float)
    sel[tie] = (k - above.sum()) / tie.sum()
    return sel


def cluster_boot(clusters: np.ndarray, reps: int, rng) -> tuple[np.ndarray, np.ndarray]:
    """Cluster bootstrap: (codes per row, replicate x cluster multiplicity matrix)."""
    codes, uniq = pd.factorize(pd.Series(clusters).fillna("__none__"))
    n = len(uniq)
    return codes, rng.multinomial(n, np.full(n, 1 / n), size=reps)


def boot_ratio(codes, counts, num, den) -> np.ndarray:
    """Bootstrap replicates of sum(num)/sum(den) with per-row arrays aggregated to clusters."""
    nc = counts.shape[1]
    cn = np.bincount(codes, weights=num, minlength=nc)
    cd = np.bincount(codes, weights=den, minlength=nc)
    d = counts @ cd
    return np.divide(counts @ cn, d, out=np.full(len(d), np.nan), where=d > 0)


def ci(x) -> list:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return [float(np.quantile(x, 0.025)), float(np.quantile(x, 0.975))] if len(x) else [np.nan, np.nan]


def fit_lr(train: pd.DataFrame, numeric: list, categorical: list):
    """The project's model spec (ml.train.make_model + fit_calibrated) for an arbitrary feature set."""
    parts = [("num", Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True)),
                               ("scale", StandardScaler())]), numeric)]
    if categorical:
        parts.append(("cat", OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=50,
                                           sparse_output=False), categorical))
    prep = ColumnTransformer(parts, verbose_feature_names_out=False)
    Xt = prep.fit_transform(train[numeric + categorical])
    clf = LogisticRegression(class_weight="balanced", C=0.5, max_iter=3000)
    cal = CalibratedClassifierCV(clf, method="sigmoid", cv=group_splits(train["cv_group"]), ensemble=False)
    cal.fit(Xt, train["label"].to_numpy(), sample_weight=train["sample_weight"].to_numpy())
    return lambda rows: cal.predict_proba(prep.transform(rows[numeric + categorical]))[:, 1]


def pr(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------------------------

class Data:
    def __init__(self):
        engine = get_engine()
        self.engine = engine
        self.assets = pd.read_parquet(ARTIFACTS / "assets.parquet")
        self.arcos = pd.read_parquet(ARTIFACTS / "arcos.parquet")
        self.storms = pd.read_parquet(ARTIFACTS / "storms.parquet").set_index("storm_id")
        self.train = pd.read_parquet(ARTIFACTS / "dataset.parquet")
        self.hops = read_frame("SELECT premise_nu, hop_seq, location_n FROM premise_upline",
                               engine, dtype={"premise_nu": str, "location_n": str})
        cust = read_frame("SELECT DISTINCT ON (premise_nu) premise_nu, circuit_key FROM customers "
                          "WHERE premise_nu IS NOT NULL ORDER BY premise_nu", engine, dtype={"premise_nu": str})
        self.homes = pd.Index(sorted(self.hops["premise_nu"].unique()), name="premise_nu")
        self.home_circuit = cust.set_index("premise_nu")["circuit_key"].reindex(self.homes).to_numpy()
        self.path_len = self.hops.groupby("premise_nu").size().reindex(self.homes).to_numpy(dtype=float)

        # Device snapping (label rule from lookup_vs_no.py).
        dev = self.assets[self.assets["asset_type"].isin(SNAP_DEVICE_TYPES) & self.assets["location_n"].notna()]
        tree = cKDTree(dev[["x", "y"]].to_numpy())
        dev_loc = dev["location_n"].astype(str).to_numpy()
        dist, idx = tree.query(self.arcos[["x", "y"]].to_numpy())
        self.arcos["snap_loc"] = dev_loc[idx]
        self.arcos["snap_m"] = dist
        _, aidx = tree.query(self.assets[["x", "y"]].to_numpy())
        self.asset_dev = dev_loc[aidx]  # each asset -> nearest device (for rolling model scores up)
        self.hops["location_n"] = self.hops["location_n"].astype(str)

        majors = self.storms[self.storms["cv_group"] != "minor_events"].sort_index()
        self.majors = list(majors.index)
        self.cv_of = self.storms["cv_group"].to_dict()

    def name(self, sid: int) -> str:
        s = self.storms.loc[sid]
        return f"{s.storm_start:%Y-%m-%d} ({s.n_records} reports)"

    def out(self, mask: pd.Series, capped: bool = False) -> np.ndarray:
        """Homes out given a boolean mask over ARCOS rows."""
        rec = self.arcos[mask.to_numpy()]
        if capped:
            rec = rec[rec["snap_m"] <= SNAP_CAP_M]
        hit = set(rec["snap_loc"])
        out_homes = self.hops.loc[self.hops["location_n"].isin(hit), "premise_nu"].unique()
        return self.homes.isin(out_homes)

    def out_storms(self, sids, capped=False) -> np.ndarray:
        return self.out(self.arcos["storm_id"].isin(list(sids)), capped)

    def rollup(self, p_asset: np.ndarray) -> np.ndarray:
        """Asset P(damage) -> P(home out) on the label's definition (nearest device, supply path)."""
        lp = np.log1p(-np.clip(p_asset, 0, 1 - 1e-12))
        dev_lp = pd.Series(lp).groupby(self.asset_dev).sum()
        home_lp = self.hops["location_n"].map(dev_lp).fillna(0.0).groupby(self.hops["premise_nu"]).sum()
        return 1 - np.exp(home_lp.reindex(self.homes).to_numpy())

    def feature_rows(self, sid: int) -> pd.DataFrame:
        s = self.storms.loc[sid]
        prior = self.arcos[self.arcos["storm_id"] < sid]
        return F.feature_rows(self.assets, prior, s.storm_start, s.storm_log_damage)

    def asset_labels(self, sid: int) -> np.ndarray:
        hit = set(self.arcos.loc[self.arcos["storm_id"] == sid, "matched_uid"].dropna())
        return self.assets["asset_uid"].isin(hit).to_numpy()

    def asset_scores(self, sid: int, feature_set: str, training: str) -> np.ndarray | None:
        """P(damage) for every asset in storm `sid` (cached). training: 'loso' or 'past'."""
        CACHE.mkdir(exist_ok=True)
        path = CACHE / f"p_{sid}_{feature_set}_{training}.npy"
        if path.exists():
            return np.load(path)
        g = self.cv_of[sid]
        if training == "loso":
            train = self.train[self.train["cv_group"] != g]
        else:
            train = self.train[self.train["storm_id"] < sid]
        if train["cv_group"].nunique() < 2:  # sigmoid calibration needs two storm groups
            return None
        t0 = time.time()
        predict = fit_lr(train, *FEATURE_SETS[feature_set])
        p = predict(self.feature_rows(sid))
        np.save(path, p)
        pr(f"    fitted {feature_set:9s} {training:4s} for {self.name(sid)} in {time.time() - t0:.0f}s")
        return p


# ---------------------------------------------------------------------------------------------
# claims 1 + 2: homes
# ---------------------------------------------------------------------------------------------

def claim_1_reproduce(d: Data) -> dict:
    """lookup_vs_no.py's exact storm windows, Helene -> February 2025."""
    day = d.arcos["report_tim"]
    hel = d.out(day.between("2024-09-25", "2024-10-03"))
    feb = d.out(day.between("2025-02-10", "2025-02-20"))
    res = {}
    for capped in (False, True):
        h = d.out(day.between("2024-09-25", "2024-10-03"), capped) if capped else hel
        f = d.out(day.between("2025-02-10", "2025-02-20"), capped) if capped else feb
        tp = int((h & f).sum())
        res["capped_60m" if capped else "as_in_lookup_vs_no"] = {
            "homes": int(len(f)), "out_helene": int(h.sum()), "out_feb": int(f.sum()),
            "warned_and_out": tp, "recall": tp / f.sum(), "precision": tp / max(h.sum(), 1),
            "always_no_accuracy": float(1 - f.mean()), "lookup_accuracy": float((h == f).mean()),
        }
    return res


def home_eval(d: Data, target: int, reps: int, rng) -> dict:
    y = d.out_storms([target])
    prev_any = target - 1
    prev_major = max([m for m in d.majors if m < target], default=None)
    lookups = {"previous_storm_any_size": d.out_storms([prev_any]),
               "all_prior_storms": d.out_storms(range(0, target))}
    if prev_major is not None:
        lookups = {"previous_major_storm": d.out_storms([prev_major]), **lookups}
    # The quote's choice: Helene -> Feb 2025 skips the 46-report January storm, i.e. "last big storm".
    prev_big = max([s for s in d.storms.index if s < target and d.storms.loc[s, "n_records"] >= BIG_STORM], default=None)
    if prev_big is not None and prev_big != prev_major:
        lookups = {"previous_storm_over_100_reports": d.out_storms([prev_big]), **lookups}

    ranked = {"path_length": d.path_len}
    for fs in ("structure", "history", "full"):
        for training in ("loso", "past"):
            p = d.asset_scores(target, fs, training)
            if p is not None:
                ranked[f"{fs}_model_{training}"] = d.rollup(p)

    codes, counts = cluster_boot(d.home_circuit, reps, rng)
    out = {"storm": d.name(target), "homes": int(len(y)), "homes_out": int(y.sum()),
           "out_rate": float(y.mean()), "circuits": int(counts.shape[1]),
           "previous_storm_any_size": d.name(prev_any),
           "previous_major_storm": d.name(prev_major) if prev_major is not None else None,
           "lookups": {}, "ranking_quality": {}}
    yf = y.astype(float)
    for lname, warn in lookups.items():
        k = int(warn.sum())
        base_boot = boot_ratio(codes, counts, warn * yf, yf)
        row = {"warnings": k, "warn_share": k / len(y), "recall": float((warn & y).sum() / y.sum()),
               "precision": float((warn & y).sum() / max(k, 1)), "recall_ci": ci(base_boot), "at_same_budget": {}}
        for rname, score in ranked.items():
            sel = soft_top(score, k)
            boot = boot_ratio(codes, counts, sel * yf, yf)
            rec = float((sel * yf).sum() / yf.sum())
            row["at_same_budget"][rname] = {
                "recall": rec, "recall_ci": ci(boot),
                "ratio_vs_lookup": rec / row["recall"] if row["recall"] > 0 else None,
                "ratio_ci": ci(boot / base_boot),
                "p_not_better": float(np.mean(boot <= base_boot)),
                "diff_pts": rec - row["recall"], "diff_ci": ci(boot - base_boot),
            }
        out["lookups"][lname] = row

    # Budget-free view: whole ranking.
    for rname, score in ranked.items():
        sel = soft_top(score, int(round(TOP_FRAC * len(y))))
        out["ranking_quality"][rname] = {
            "roc_auc": float(roc_auc_score(y, score)),
            "avg_precision": float(average_precision_score(y, score)), "base_rate": float(y.mean()),
            "recall_top5pct_homes": float((sel * yf).sum() / yf.sum()),
            "recall_top5pct_ci": ci(boot_ratio(codes, counts, sel * yf, yf)),
        }
    out["_scores"] = ranked  # used by claim 4 (confidence), stripped before saving
    out["_y"] = y
    return out


def label_sensitivity(d: Data, target: int, reps: int, rng) -> dict:
    """Headline comparison with the 60 m snap cap on labels."""
    y = d.out_storms([target], capped=True)
    prev_major = max(m for m in d.majors if m < target)
    warn = d.out_storms([prev_major], capped=True)
    k = int(warn.sum())
    codes, counts = cluster_boot(d.home_circuit, reps, rng)
    yf = y.astype(float)
    base = boot_ratio(codes, counts, warn * yf, yf)
    res = {"homes_out": int(y.sum()), "lookup_warnings": k, "lookup_recall": float((warn & y).sum() / y.sum()),
           "lookup_recall_ci": ci(base), "at_same_budget": {}}
    for rname, score in {"path_length": d.path_len,
                         "structure_model_loso": d.rollup(d.asset_scores(target, "structure", "loso")),
                         "full_model_loso": d.rollup(d.asset_scores(target, "full", "loso"))}.items():
        sel = soft_top(score, k)
        b = boot_ratio(codes, counts, sel * yf, yf)
        rec = float((sel * yf).sum() / yf.sum())
        res["at_same_budget"][rname] = {"recall": rec, "ratio_vs_lookup": rec / res["lookup_recall"],
                                        "ratio_ci": ci(b / base)}
    return res


# ---------------------------------------------------------------------------------------------
# claim 3: equipment
# ---------------------------------------------------------------------------------------------

def asset_eval(d: Data, reps: int, rng) -> dict:
    res = {}
    n = len(d.assets)
    k = int(round(TOP_FRAC * n))
    codes, counts = cluster_boot(d.assets["circuit_key"].to_numpy(), reps, rng)
    for sid in d.majors:
        y = d.asset_labels(sid).astype(float)
        rows = d.feature_rows(sid)
        prev_major = max([m for m in d.majors if m < sid], default=None)
        ranked = {"nearby_past_damage_250m": rows["hist_share_250m"].to_numpy(),
                  "circuit_past_damage": rows["hist_circuit_share"].to_numpy(),
                  **type_rules(d.train[d.train["cv_group"] != d.cv_of[sid]], rows)}
        for fs in ("structure", "history", "full"):
            for training in ("loso", "past"):
                p = d.asset_scores(sid, fs, training)
                if p is not None:
                    ranked[f"{fs}_model_{training}"] = p
        fold = {"storm": d.name(sid), "damaged_assets": int(y.sum()), "assets": n, "top5pct": {},
                "lookups": {}}
        for rname, score in ranked.items():
            sel = soft_top(score, k)
            fold["top5pct"][rname] = {"recall": float((sel * y).sum() / y.sum()),
                                      "recall_ci": ci(boot_ratio(codes, counts, sel * y, y)),
                                      "roc_auc": float(roc_auc_score(y, score))}
        # Literal "repeat" lookups at equipment level: warn only what was damaged before.
        lk = {"damaged_in_any_prior_storm": rows["hist_asset_damaged"].to_numpy() > 0}
        if prev_major is not None:
            lk["damaged_in_previous_major_storm"] = d.asset_labels(prev_major)
        for lname, warn in lk.items():
            kw = int(warn.sum())
            full = soft_top(ranked["full_model_loso"], kw)
            fold["lookups"][lname] = {"warnings": kw, "recall": float((warn * y).sum() / y.sum()),
                                      "full_model_recall_same_budget": float((full * y).sum() / y.sum())}
        res[d.cv_of[sid]] = fold

    # Minor events, pooled, evaluated like ml.train (10% of negatives, weighted back).
    ids = d.storms.index[d.storms["cv_group"] == "minor_events"]
    test = F.make_rows(d.assets, d.arcos, d.storms.reset_index(), ids, neg_rate=EVAL_NEG_RATE_MINOR, seed=1234)
    train = d.train[d.train["cv_group"] != "minor_events"]
    y, w = test["label"].to_numpy(), test["sample_weight"].to_numpy()
    fold = {"storm": f"{len(ids)} minor events pooled", "damaged_assets": int(y.sum()), "top5pct": {}}
    for fs in ("structure", "history", "full"):
        path = CACHE / f"p_minor_{fs}.npy"
        if path.exists():
            p = np.load(path)
        else:
            p = fit_lr(train, *FEATURE_SETS[fs])(test)
            np.save(path, p)
        fold["top5pct"][f"{fs}_model_loso"] = {"recall": topk_recall(y, p, w, TOP_FRAC),
                                               "roc_auc": float(roc_auc_score(y, p, sample_weight=w))}
    fold["top5pct"]["nearby_past_damage_250m"] = {
        "recall": topk_recall(y, test["hist_share_250m"].to_numpy(), w, TOP_FRAC)}
    for rn, score in type_rules(train, test).items():
        # tiny jitter = random tie-break inside topk_recall
        jitter = np.random.default_rng(SEED).random(len(score)) * 1e-12
        fold["top5pct"][rn] = {"recall": topk_recall(y, score + jitter, w, TOP_FRAC)}
    res["minor_events"] = fold

    folds = list(res)
    res["mean_over_4_folds"] = {
        r: float(np.mean([res[g]["top5pct"][r]["recall"] for g in folds]))
        for r in ("full_model_loso", "structure_model_loso", "history_model_loso", "nearby_past_damage_250m",
                  "type_rate", "type_rate_x_length")}
    return res


def type_rules(train: pd.DataFrame, rows: pd.DataFrame) -> dict:
    """No-model baselines: each asset type's damage rate in the training storms (x wire length for lines)."""
    w = train["sample_weight"]
    rate = (train["label"] * w).groupby(train["asset_type"]).sum() / w.groupby(train["asset_type"]).sum()
    r = rows["asset_type"].map(rate).fillna(0).to_numpy()
    seg = rows["segment_len_m"].to_numpy()
    mean_len = rows.groupby("asset_type")["segment_len_m"].transform("mean").to_numpy()
    length = np.where(np.isfinite(seg) & (mean_len > 0), seg / np.where(mean_len > 0, mean_len, 1), 1.0)
    return {"type_rate": r, "type_rate_x_length": r * length}


# ---------------------------------------------------------------------------------------------
# claim 4: restoration time and "how sure we are"
# ---------------------------------------------------------------------------------------------

def duration_backtest(reps: int, rng) -> dict:
    """ml.duration's leave-one-storm-out check, plus a constant-interval baseline and CIs."""
    arcos = pd.read_parquet(ARTIFACTS / "arcos.parquet")
    storms = D.cluster_storms(arcos)
    df = D.prepare(arcos, storms)
    cats = {c: sorted(df[c].unique()) for c in D.CATEGORICAL}
    y = np.log1p(df["repair_days"].to_numpy())
    res = {}
    for g in sorted(df["cv_group"].unique()):
        tr, te = (df["cv_group"] != g).to_numpy(), (df["cv_group"] == g).to_numpy()
        q = {a: np.expm1(D.make_model(a).fit(D.to_frame(df[tr], cats), y[tr]).predict(D.to_frame(df[te], cats)))
             for a in D.QUANTILES}
        lo, mid, hi = np.floor(q[0.1]), q[0.5], np.ceil(q[0.9])
        act = df.loc[te, "repair_days"].to_numpy()
        blo, bmid, bhi = (df.loc[tr, "repair_days"].quantile(a) for a in D.QUANTILES)
        cov = ((act >= lo) & (act <= hi)).astype(float)
        bcov = ((act >= blo) & (act <= bhi)).astype(float)
        idx = rng.integers(0, len(act), size=(reps, len(act)))
        res[g] = {
            "records": int(te.sum()), "nominal_coverage": 0.8,
            "model_coverage": float(cov.mean()), "model_coverage_ci": ci(cov[idx].mean(axis=1)),
            "constant_coverage": float(bcov.mean()),
            "model_median_width_days": float(np.median(hi - lo)), "constant_width_days": float(bhi - blo),
            "pinball_q50_model": float(mean_pinball_loss(act, mid, alpha=0.5)),
            "pinball_q50_constant": float(mean_pinball_loss(act, np.full(len(act), bmid), alpha=0.5)),
            "actual_median_days": float(np.median(act)), "actual_p90_days": float(np.quantile(act, 0.9)),
        }
    return res


def home_restore_days(d: Data, sid: int) -> pd.Series:
    """Days from report to repair for each home out in storm `sid` (the slowest repair on its path)."""
    rec = d.arcos[d.arcos["storm_id"] == sid].copy()
    rec["days"] = np.where(rec["repaired"] == "Yes", (rec["repaired_d"] - rec["report_tim"]).dt.days, np.nan)
    by_dev = rec.groupby("snap_loc")["days"].agg(lambda s: np.nan if s.isna().any() else s.max())
    h = d.hops[d.hops["location_n"].isin(by_dev.index)]
    days = h["location_n"].map(by_dev).groupby(h["premise_nu"]).agg(lambda s: np.nan if s.isna().any() else s.max())
    return days.dropna()


def restoration_backtest(d: Data, target: int, n_homes: int, n_runs: int, rng) -> dict:
    """The app's RestorationEstimator vs. what actually happened in the February 2025 storm."""
    from ml.score import PremiseScorer
    from restoration import RestorationEstimator

    actual = home_restore_days(d, target)
    prior = pd.concat([home_restore_days(d, m) for m in d.majors if m < target])
    plo, phi = (np.quantile(prior, a) * 24 for a in (0.1, 0.9))
    scorer = PremiseScorer(d.engine)
    est = RestorationEstimator(d.engine, scorer)
    sample = actual.sample(min(n_homes, len(actual)), random_state=SEED)
    rows = []
    for prem, days in sample.items():
        r = est.estimate(prem, {"storm_severity": "high"}, n_runs=n_runs)
        if r["fallback"]:
            continue
        # Report and repair dates are whole days, so the true elapsed time is within +/- 1 day of the gap.
        lo_a, hi_a = max(days - 1, 0) * 24, (days + 1) * 24
        rows.append({"days": days, "p10": r["p10_hrs"], "p50": r["p50_hrs"], "p90": r["p90_hrs"],
                     "covered": r["p10_hrs"] <= hi_a and r["p90_hrs"] >= lo_a,
                     "p50_in_window": lo_a <= r["p50_hrs"] <= hi_a,
                     "const_covered": plo <= hi_a and phi >= lo_a,
                     "circuit": d.home_circuit[d.homes.get_loc(prem)]})
    t = pd.DataFrame(rows)
    # Fairer constant: give every home the model's own typical interval (tests the per-home information).
    mlo, mhi = t["p10"].median(), t["p90"].median()
    lo_a, hi_a = np.maximum(t["days"] - 1, 0) * 24, (t["days"] + 1) * 24
    t["same_for_all_covered"] = (mlo <= hi_a) & (mhi >= lo_a)
    rho = spearmanr(t["p50"], t["days"])
    codes, counts = cluster_boot(t["circuit"].to_numpy(), 1000, rng)
    one = np.ones(len(t))
    return {
        "storm": d.name(target), "homes_out_with_repair_dates": int(len(actual)), "homes_scored": int(len(t)),
        "actual_days_median": float(t["days"].median()), "actual_days_p10_p90": [float(t["days"].quantile(0.1)),
                                                                                 float(t["days"].quantile(0.9))],
        "estimate_hours_median_p10_p50_p90": [float(t[c].median()) for c in ("p10", "p50", "p90")],
        "interval_covers_actual": float(t["covered"].mean()),
        "interval_covers_actual_ci": ci(boot_ratio(codes, counts, t["covered"].to_numpy(float), one)),
        "p50_within_a_day": float(t["p50_in_window"].mean()),
        "constant_interval_hours": [float(plo), float(phi)],
        "constant_interval_covers_actual": float(t["const_covered"].mean()),
        "same_interval_for_all_hours": [float(mlo), float(mhi)],
        "same_interval_for_all_covers_actual": float(t["same_for_all_covered"].mean()),
        "actual_days_distribution": {int(k): int(v) for k, v in t["days"].value_counts().sort_index().items()},
        "spearman_p50_vs_actual": float(rho.statistic), "spearman_p": float(rho.pvalue),
        "caveat": "asset_risk is scored with history that includes this storm, and the default 2 crews per "
                  "circuit is an assumption; both favour or distort the estimate rather than penalize it.",
    }


def confidence_check(d: Data, target: int, home: dict) -> dict:
    """Does the app's confidence label (records on the home's circuit) track accuracy? Prior records only."""
    prior = d.arcos[d.arcos["storm_id"] < target]
    n_circ = pd.Series(d.home_circuit).map(prior["circuit_key"].value_counts()).fillna(0).to_numpy()
    band = np.where(n_circ >= 20, "High", np.where(n_circ >= 5, "Medium", "Low"))
    y, p = home["_y"], home["_scores"]["full_model_loso"]
    out = {}
    for b in ("High", "Medium", "Low"):
        m = band == b
        yy, pp = y[m], p[m]
        out[b] = {"homes": int(m.sum()), "circuits": int(pd.Series(d.home_circuit[m]).nunique()),
                  "out_rate": float(yy.mean()) if m.any() else None,
                  "mean_predicted": float(pp.mean()) if m.any() else None,
                  "roc_auc": float(roc_auc_score(yy, pp)) if 0 < yy.sum() < m.sum() else None,
                  "brier": float(np.mean((pp - yy) ** 2)) if m.any() else None,
                  "brier_base_rate": float(np.mean((yy.mean() - yy) ** 2)) if m.any() else None}
    return out


# ---------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------

def pct(x):
    return "n/a" if x is None or not np.isfinite(x) else f"{x:.1%}"


def rng_s(c):
    return f"[{pct(c[0])}, {pct(c[1])}]"


def report(R: dict) -> None:
    c1 = R["claim1_reproduce"]["as_in_lookup_vs_no"]
    pr("\n=== (1) 'last storm repeats' warned 3.6% of homes that lost power ===")
    pr(f"  reproduced: {c1['warned_and_out']:,} of {c1['out_feb']:,} homes out in Feb 2025 were out in Helene "
       f"-> recall {pct(c1['recall'])} (precision {pct(c1['precision'])}, warned {c1['out_helene']:,})")
    c1b = R["claim1_reproduce"]["capped_60m"]
    pr(f"  with 60 m snap cap on labels: recall {pct(c1b['recall'])}")
    for g, h in R["homes"].items():
        pr(f"  target {h['storm']}: {h['homes_out']:,} homes out ({pct(h['out_rate'])}), {h['circuits']} circuits")
        for ln, l in h["lookups"].items():
            pr(f"    lookup '{ln}': warns {l['warnings']:,} homes, recall {pct(l['recall'])} {rng_s(l['recall_ci'])}")

    pr("\n=== (2) grid structure beats it by half at the same number of warnings ===")
    pr("=== (+) model vs lookup at the same number of warnings ===")
    for g, h in R["homes"].items():
        for ln, l in h["lookups"].items():
            pr(f"  {h['storm']} | budget = lookup '{ln}' ({l['warnings']:,} warnings, recall {pct(l['recall'])})")
            for rn, r in l["at_same_budget"].items():
                ratio = r["ratio_vs_lookup"]
                pr(f"     {rn:22s} recall {pct(r['recall']):>6s} {rng_s(r['recall_ci']):18s} "
                   f"x{ratio:.2f} [{r['ratio_ci'][0]:.2f}, {r['ratio_ci'][1]:.2f}]  "
                   f"diff {r['diff_pts'] * 100:+.1f} pts [{r['diff_ci'][0] * 100:+.1f}, {r['diff_ci'][1] * 100:+.1f}]  "
                   f"P(not better) {r['p_not_better']:.2f}" if ratio is not None else f"     {rn}: lookup recall 0")
        pr(f"  {h['storm']} ranking quality (ROC-AUC / avg precision / recall in top 5% of homes):")
        for rn, q in h["ranking_quality"].items():
            pr(f"     {rn:22s} {q['roc_auc']:.3f} / {q['avg_precision']:.3f} (base {q['base_rate']:.3f}) / "
               f"{pct(q['recall_top5pct_homes'])} {rng_s(q['recall_top5pct_ci'])}")
    s = R["label_sensitivity_60m"]
    pr(f"  label sensitivity (60 m cap): lookup recall {pct(s['lookup_recall'])}; "
       + ", ".join(f"{k} x{v['ratio_vs_lookup']:.2f} [{v['ratio_ci'][0]:.2f}, {v['ratio_ci'][1]:.2f}]"
                   for k, v in s["at_same_budget"].items()))

    pr("\n=== (3) riskiest 5% of equipment held a third of the damage (unseen storms) ===")
    for g, f in R["assets"].items():
        if g == "mean_over_4_folds":
            continue
        pr(f"  {f['storm']}: {f['damaged_assets']} damaged assets")
        for rn, r in f["top5pct"].items():
            extra = f" {rng_s(r['recall_ci'])}" if "recall_ci" in r else ""
            auc = f"  AUC {r['roc_auc']:.3f}" if "roc_auc" in r else ""
            pr(f"     {rn:24s} top-5% recall {pct(r['recall'])}{extra}{auc}")
        for ln, l in f.get("lookups", {}).items():
            pr(f"     lookup '{ln}': {l['warnings']} warnings, recall {pct(l['recall'])}; full model at same "
               f"budget {pct(l['full_model_recall_same_budget'])}")
    pr("  mean over the 4 folds (as in METRICS.md): "
       + ", ".join(f"{k} {pct(v)}" for k, v in R["assets"]["mean_over_4_folds"].items()))

    pr("\n=== (4a) repair-duration intervals (P10-P90, nominal 80%) ===")
    for g, r in R["duration"].items():
        pr(f"  {g:14s} n={r['records']:4d} model coverage {pct(r['model_coverage'])} {rng_s(r['model_coverage_ci'])}"
           f"  constant {pct(r['constant_coverage'])}  pinball q50 model {r['pinball_q50_model']:.2f} vs "
           f"constant {r['pinball_q50_constant']:.2f}  (actual median {r['actual_median_days']:.0f} d, "
           f"P90 {r['actual_p90_days']:.0f} d)")
    if R.get("restoration"):
        r = R["restoration"]
        pr("=== (4b) restoration estimator vs. February 2025 ===")
        pr(f"  {r['homes_scored']} homes; actual median {r['actual_days_median']:.0f} days "
           f"(P10-P90 {r['actual_days_p10_p90']}); estimate median P10/P50/P90 hours "
           f"{r['estimate_hours_median_p10_p50_p90']}")
        pr(f"  P10-P90 covers actual (+/-1 day): {pct(r['interval_covers_actual'])} {rng_s(r['interval_covers_actual_ci'])}"
           f"; P50 within a day: {pct(r['p50_within_a_day'])}; constant interval {r['constant_interval_hours']} h "
           f"covers {pct(r['constant_interval_covers_actual'])}; same interval for every home "
           f"{r['same_interval_for_all_hours']} h covers {pct(r['same_interval_for_all_covers_actual'])}; "
           f"Spearman(P50, actual) {r['spearman_p50_vs_actual']:.2f} (p={r['spearman_p']:.2f})")
        pr(f"  actual repair days (homes): {r['actual_days_distribution']}")
    pr("=== (4c) confidence label vs. accuracy (Feb 2025 homes) ===")
    for b, r in R["confidence"].items():
        auc = "n/a" if r["roc_auc"] is None else f"{r['roc_auc']:.3f}"
        pr(f"  {b:6s} {r['homes']:6,} homes / {r['circuits']} circuits: out {pct(r['out_rate'])}, "
           f"mean predicted {pct(r['mean_predicted'])}, AUC {auc}, Brier {r['brier']:.4f} "
           f"(base-rate {r['brier_base_rate']:.4f})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="fewer bootstrap reps and restoration homes")
    ap.add_argument("--no-restoration", action="store_true")
    args = ap.parse_args()
    reps = 300 if args.quick else 2000
    rng = np.random.default_rng(SEED)
    np.seterr(divide="ignore", invalid="ignore")  # ratio replicates where the lookup caught nothing
    t0 = time.time()

    d = Data()
    pr(f"{len(d.homes):,} homes with a supply path, {len(d.assets):,} assets, {len(d.storms)} storms; "
       f"major: {', '.join(d.name(m) for m in d.majors)}")
    R = {"claim1_reproduce": claim_1_reproduce(d), "homes": {}}
    for target in d.majors:
        R["homes"][d.cv_of[target]] = home_eval(d, target, reps, rng)
    feb = d.majors[-1]
    R["label_sensitivity_60m"] = label_sensitivity(d, feb, reps, rng)
    R["assets"] = asset_eval(d, reps, rng)
    R["duration"] = duration_backtest(reps, rng)
    if not args.no_restoration:
        R["restoration"] = restoration_backtest(d, feb, 80 if args.quick else 400, 300 if args.quick else 1000, rng)
    R["confidence"] = confidence_check(d, feb, R["homes"][d.cv_of[feb]])
    for h in R["homes"].values():
        h.pop("_scores")
        h.pop("_y")

    report(R)
    OUT.mkdir(exist_ok=True)
    (OUT / "quote_check.json").write_text(json.dumps(R, indent=2, default=float))
    pr(f"\nWrote {OUT / 'quote_check.json'} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
