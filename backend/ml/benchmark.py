"""Benchmark candidate risk and repair-time models against the production ones.

    python -m ml.benchmark            # writes artifacts/BENCHMARK.md and benchmark.json

Same protocol as ml.train: leave-one-major-storm-out, every asset of the held-out storm scored, each
candidate sigmoid-calibrated with storm-grouped inner CV (so Brier is comparable), metrics from
train.fold_metrics. A candidate replaces production only if it clears the adoption rule in ADOPT.
"""
import json
import time

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import mean_pinball_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, OrdinalEncoder, StandardScaler

from . import features as F
from . import train as T
from .db import ARTIFACTS

ADOPT = {"min_lift_gain": 0.10, "max_roc_drop": 0.01}  # plus: Brier not worse, Feb 2025 fold not worse
CANDIDATES = ["baseline_lr", "physics_lr", "lgbm_monotone", "ebm", "ensemble"]
LGBM_GRID = [{"num_leaves": nl, "min_child_samples": mc} for nl in (7, 15) for mc in (200, 1000)]
MONOTONE_UP = {"storm_log_damage", "hist_asset_damaged", "hist_circuit_share", "hist_share_250m",
               "hist_share_500m", "age_years", "segment_len_m"}


# ---- physics-informed features (wind load on structures and spans) -----------------------------------

PHYSICS = ["phys_pole_stress", "phys_span_exposure", "phys_overhead_x_storm", "phys_age_x_storm",
           "phys_bare_x_storm", "phys_span_x_storm", "phys_history_x_storm"]


def add_physics(X: pd.DataFrame) -> pd.DataFrame:
    """Wind-load proxies from existing columns; NaN stays NaN (imputed + indicated downstream).

    - pole stress ~ height^2 x class number (ground-line bending moment grows with height^2; higher ANSI
      class numbers are weaker poles)
    - span exposure ~ overhead span length (wind force on a conductor scales with its exposed length)
    - storm interactions: the same weakness matters more in a bigger storm (a fragility curve's slope)
    """
    X = X.copy()
    s = X["storm_log_damage"]
    X["phys_pole_stress"] = X["height_ft"] ** 2 * X["pole_class_num"] / 1000
    X["phys_span_exposure"] = X["segment_len_m"] * X["overhead"]
    X["phys_overhead_x_storm"] = X["overhead"] * s
    X["phys_age_x_storm"] = X["age_years"] * s
    X["phys_bare_x_storm"] = X["bare_conductor"] * s
    X["phys_span_x_storm"] = np.log1p(X["segment_len_m"]) * s
    X["phys_history_x_storm"] = X["hist_share_250m"] * s
    return X


def _physics_names(_, names):
    return list(names) + PHYSICS


def _lr_prep(numeric: list[str], physics: bool):
    cols = ColumnTransformer([
        ("num", Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True)),
                          ("scale", StandardScaler())]), numeric + (PHYSICS if physics else [])),
        ("cat", OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=50, sparse_output=False),
         F.CATEGORICAL),
    ], verbose_feature_names_out=False)
    if not physics:
        return cols
    return Pipeline([("physics", FunctionTransformer(add_physics, feature_names_out=_physics_names)),
                     ("cols", cols)])


class Candidate(BaseEstimator, ClassifierMixin):
    """One candidate model behind a uniform fit(X, y, sample_weight) so CalibratedClassifierCV can wrap it."""

    def __init__(self, name: str = "baseline_lr", params: dict | None = None):
        self.name = name
        self.params = params

    def fit(self, X, y, sample_weight=None):
        numeric = F.NUMERIC
        if self.name in ("baseline_lr", "physics_lr"):
            self.model_ = Pipeline([("prep", _lr_prep(numeric, self.name == "physics_lr")),
                                    ("clf", LogisticRegression(class_weight="balanced", C=0.5, max_iter=3000))])
        elif self.name == "lgbm_monotone":
            p = {"num_leaves": 15, "min_child_samples": 200, **(self.params or {})}
            mono = [1 if f in MONOTONE_UP else 0 for f in numeric] + [0] * len(F.CATEGORICAL)
            ordinal = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1, encoded_missing_value=-1)
            self.model_ = Pipeline([
                ("prep", ColumnTransformer([("num", "passthrough", numeric), ("cat", ordinal, F.CATEGORICAL)])),
                ("clf", LGBMClassifier(n_estimators=300, learning_rate=0.03, subsample=0.8, subsample_freq=1,
                                       colsample_bytree=0.7, reg_lambda=5.0, monotone_constraints=mono,
                                       monotone_constraints_method="advanced", random_state=0, verbose=-1, **p))])
        elif self.name == "ebm":
            from interpret.glassbox import ExplainableBoostingClassifier
            self.model_ = Pipeline([("clf", ExplainableBoostingClassifier(
                interactions=10, outer_bags=4, max_rounds=3000, random_state=0, n_jobs=-1))])
        else:
            raise ValueError(self.name)
        X = X[numeric + F.CATEGORICAL]
        if self.name == "ebm":
            X = X.astype({c: "string" for c in F.CATEGORICAL}).fillna({c: "missing" for c in F.CATEGORICAL})
            # EBM has no class_weight: rebalance through the weights (same effect as class_weight="balanced").
            w = np.asarray(sample_weight, float)
            pos = w[np.asarray(y) == 1].sum()
            w = np.where(np.asarray(y) == 1, w * (w.sum() - pos) / pos, w)
            self.model_.fit(X, y, clf__sample_weight=w)
        else:
            self.model_.fit(X, y, clf__sample_weight=sample_weight)
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, X):
        X = X[F.NUMERIC + F.CATEGORICAL]
        if self.name == "ebm":
            X = X.astype({c: "string" for c in F.CATEGORICAL}).fillna({c: "missing" for c in F.CATEGORICAL})
        return self.model_.predict_proba(X)

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


def calibrated(name: str, train: pd.DataFrame, params: dict | None = None) -> CalibratedClassifierCV:
    cal = CalibratedClassifierCV(Candidate(name, params), method="sigmoid",
                                 cv=T.group_splits(train["cv_group"]), ensemble=False)
    cal.fit(train, train["label"].to_numpy(), sample_weight=train["sample_weight"].to_numpy())
    return cal


def pick_lgbm_params(train: pd.DataFrame) -> dict:
    """Nested search: score each grid point by inner leave-one-group-out PR-AUC lift on the training storms."""
    best = None
    for params in LGBM_GRID:
        lifts = []
        for tr, va in T.group_splits(train["cv_group"]):
            a, b = train.iloc[tr], train.iloc[va]
            if b["label"].sum() == 0:
                continue
            m = Candidate("lgbm_monotone", params).fit(a, a["label"], a["sample_weight"])
            lifts.append(T.fold_metrics(b["label"].to_numpy(), m.predict_proba(b)[:, 1],
                                        b["sample_weight"].to_numpy())["pr_auc_lift"])
        score = float(np.mean(lifts))
        if best is None or score > best[0]:
            best = (score, params)
    return best[1]


def test_sets(assets, arcos, storms, folds) -> dict:
    """Full held-out evaluation sets (as in ml.train), cached between runs."""
    out = {}
    for g in folds:
        path = ARTIFACTS / f"bench_test_{g}.parquet"
        if path.exists():
            out[g] = pd.read_parquet(path)
            continue
        ids = storms.loc[storms["cv_group"] == g, "storm_id"]
        rate = T.EVAL_NEG_RATE_MINOR if g == "minor_events" else 1.0
        out[g] = F.make_rows(assets, arcos, storms, ids, neg_rate=rate, seed=1234)
        out[g].to_parquet(path, index=False)
    return out


def risk_benchmark(only: list[str] | None = None) -> dict:
    data = pd.read_parquet(ARTIFACTS / "dataset.parquet")
    assets = pd.read_parquet(ARTIFACTS / "assets.parquet")
    arcos = pd.read_parquet(ARTIFACTS / "arcos.parquet")
    storms = pd.read_parquet(ARTIFACTS / "storms.parquet")
    folds = sorted(storms["cv_group"].unique())
    tests = test_sets(assets, arcos, storms, folds)
    names = only or CANDIDATES
    results, preds, chosen = {n: {} for n in names}, {n: {} for n in names}, {}
    t0 = time.time()
    for name in [n for n in names if n != "ensemble"]:
        for g in folds:
            train, test = data[data["cv_group"] != g], tests[g]
            params = None
            if name == "lgbm_monotone":
                params = chosen.setdefault(g, pick_lgbm_params(train))
            p = calibrated(name, train, params).predict_proba(test)[:, 1]
            preds[name][g] = p
            results[name][g] = T.fold_metrics(test["label"].to_numpy(), p, test["sample_weight"].to_numpy())
        print(f"  {name:15s} done ({time.time() - t0:.0f}s)", flush=True)
    if "ensemble" in names:
        lin = max((n for n in ("baseline_lr", "physics_lr") if n in preds and preds[n]), key=lambda n: _mean(results[n]))
        tree = max((n for n in ("lgbm_monotone", "ebm") if n in preds and preds[n]), key=lambda n: _mean(results[n]))
        for g in folds:
            p = (preds[lin][g] + preds[tree][g]) / 2
            results["ensemble"][g] = T.fold_metrics(tests[g]["label"].to_numpy(), p, tests[g]["sample_weight"].to_numpy())
        results["ensemble"]["members"] = [lin, tree]
    for name in names:
        fold_keys = [g for g in folds if g in results[name]]
        results[name]["mean"] = {k: float(np.mean([results[name][g][k] for g in fold_keys]))
                                 for k in results[name][fold_keys[0]]}
    return {"folds": folds, "results": results, "lgbm_params": chosen}


def _mean(r: dict) -> float:
    return float(np.mean([v["pr_auc_lift"] for k, v in r.items() if isinstance(v, dict) and "pr_auc_lift" in v]))


def adoption(results: dict, base: str = "baseline_lr") -> dict:
    b = results[base]["mean"]
    feb = "storm_2025-02"
    verdict = {}
    for name, r in results.items():
        if name == base:
            continue
        m = r["mean"]
        checks = {
            "lift_gain": m["pr_auc_lift"] >= b["pr_auc_lift"] * (1 + ADOPT["min_lift_gain"]),
            "brier_not_worse": m["brier"] <= b["brier"] * 1.001,
            "roc_auc_ok": m["roc_auc"] >= b["roc_auc"] - ADOPT["max_roc_drop"],
            "feb2025_not_worse": r[feb]["pr_auc_lift"] >= results[base][feb]["pr_auc_lift"],
        }
        verdict[name] = {"checks": checks, "adopt": all(checks.values()),
                         "lift_change": m["pr_auc_lift"] / b["pr_auc_lift"] - 1}
    return verdict


# ---- repair time ----------------------------------------------------------------------------------------

def duration_benchmark() -> dict:
    """Current quantile LightGBM vs. + storm/weather features vs. a censored Weibull AFT, per held-out storm."""
    from scipy.optimize import minimize

    from . import duration as D
    from .weather import load_weather, storm_weather

    arcos = pd.read_parquet(ARTIFACTS / "arcos.parquet")
    storms = F.cluster_storms(arcos)
    sw = storm_weather(storms, load_weather(arcos)).groupby("storm_id").agg(
        storm_gust=("gust_mph", "max"), storm_snow=("snow_in", "max")).reset_index()
    df = D.prepare(arcos, storms).merge(storms[["storm_id", "storm_log_damage"]], on="storm_id").merge(
        sw, on="storm_id", how="left")
    # Unrepaired (censored) records: still open at the end of the extract.
    end = arcos["report_tim"].max()
    cen = arcos[arcos["repaired"] != "Yes"].merge(storms[["storm_id", "cv_group", "storm_log_damage"]], on="storm_id")
    cen = cen.merge(sw, on="storm_id", how="left")
    cen["repair_days"] = (end - cen["report_tim"]).dt.days.clip(lower=0)
    cen["asset_type"] = cen["arcos_asset_type"].fillna("unknown")
    cen["access_issue"] = (cen["access_iss"] == "Yes").astype(int)
    cen["special_equipment"] = cen["special_eq"].fillna("none")
    categories = {c: sorted(df[c].unique()) for c in D.CATEGORICAL}

    def lgbm_preds(tr, te, extra):
        cols = D.FEATURES + extra
        def frame(d):
            X = d[cols].copy()
            for c in D.CATEGORICAL:
                X[c] = pd.Categorical(X[c], categories=categories[c])
            return X
        y = np.log1p(tr["repair_days"].to_numpy())
        return {q: np.expm1(D.make_model(q).fit(frame(tr), y).predict(frame(te))) for q in D.QUANTILES}

    def design(d, levels):
        X = [np.ones(len(d)), d["access_issue"].to_numpy(float), d["storm_log_damage"].to_numpy(float)]
        X += [(d["asset_type"] == lv).to_numpy(float) for lv in levels]
        return np.column_stack(X)

    def aft_preds(tr, tr_cen, te):
        levels = [lv for lv in categories["asset_type"] if (tr["asset_type"] == lv).sum() >= 20][1:]
        Xo, Xc = design(tr, levels), design(tr_cen, levels)
        lo_, lc_ = np.log1p(tr["repair_days"].to_numpy(float)), np.log1p(tr_cen["repair_days"].to_numpy(float))

        def nll(theta):  # Weibull AFT: log(1 + days) = X beta + sigma * eps, eps ~ standard min-Gumbel
            beta, sigma = theta[:-1], np.exp(theta[-1])
            zo, zc = (lo_ - Xo @ beta) / sigma, (lc_ - Xc @ beta) / sigma
            return -(np.sum(zo - np.exp(zo) - np.log(sigma)) - np.sum(np.exp(zc)))
        th = minimize(nll, np.r_[np.full(Xo.shape[1], 0.0), 0.0], method="L-BFGS-B").x
        beta, sigma = th[:-1], np.exp(th[-1])
        lin = design(te, levels) @ beta
        return {q: np.expm1(lin + sigma * np.log(-np.log(1 - q))) for q in D.QUANTILES}

    rows = {}
    for g in sorted(df["cv_group"].unique()):
        tr, te = df[df["cv_group"] != g], df[df["cv_group"] == g]
        actual = te["repair_days"].to_numpy()
        cands = {
            "baseline_constant": {q: np.full(len(te), tr["repair_days"].quantile(q)) for q in D.QUANTILES},
            "current_lgbm": lgbm_preds(tr, te, []),
            "lgbm_storm_weather": lgbm_preds(tr, te, ["storm_log_damage", "storm_gust", "storm_snow"]),
            "weibull_aft_censored": aft_preds(tr, cen[cen["cv_group"] != g], te),
        }
        rows[g] = {}
        for name, p in cands.items():
            lo, hi = np.floor(p[0.1]), np.ceil(p[0.9])
            rows[g][name] = {"n": int(len(te)), "coverage": float(np.mean((actual >= lo) & (actual <= hi))),
                             **{f"pinball_q{int(q * 100)}": float(mean_pinball_loss(actual, p[q], alpha=q))
                                for q in D.QUANTILES}}
    names = list(next(iter(rows.values())))
    mean = {n: {k: float(np.mean([rows[g][n][k] for g in rows])) for k in rows[next(iter(rows))][n] if k != "n"}
            for n in names}
    return {"folds": rows, "mean": mean, "n_censored": int(len(cen))}


# ---- report ---------------------------------------------------------------------------------------------

def write_report(risk: dict, verdict: dict, dur: dict | None, hazard: dict | None, fno: dict | None) -> None:
    lines = ["# Model benchmark", "",
             "Protocol: leave-one-major-storm-out (same folds, test sets and metrics as `METRICS.md`). Every",
             "candidate is sigmoid-calibrated with storm-grouped inner CV. Adoption rule: mean PR-AUC lift",
             f"≥ {ADOPT['min_lift_gain']:.0%} higher, Brier not worse, ROC-AUC at most {ADOPT['max_roc_drop']} lower, "
             "and the Feb 2025 storm not worse.", "", "## Outage-risk models (mean across folds)", "",
             "| Model | PR-AUC lift | ROC-AUC | Brier | Recall top 1% | Recall top 5% | Adopt? |",
             "|---|---|---|---|---|---|---|"]
    for name, r in risk["results"].items():
        m = r["mean"]
        adopt = "baseline" if name == "baseline_lr" else ("**yes**" if verdict[name]["adopt"] else "no")
        lines.append(f"| {name} | {m['pr_auc_lift']:.2f}x | {m['roc_auc']:.3f} | {m['brier']:.5f} | "
                     f"{m['recall_top1pct']:.2f} | {m['recall_top5pct']:.2f} | {adopt} |")
    lines += ["", "### Per held-out storm (PR-AUC lift)", "",
              "| Model | " + " | ".join(risk["folds"]) + " |", "|---|" + "---|" * len(risk["folds"])]
    for name, r in risk["results"].items():
        lines.append(f"| {name} | " + " | ".join(f"{r[g]['pr_auc_lift']:.2f}x" for g in risk["folds"]) + " |")
    lines += ["", "### Adoption checks", ""]
    for name, v in verdict.items():
        failed = [k for k, ok in v["checks"].items() if not ok]
        lines.append(f"- **{name}**: lift {v['lift_change']:+.0%} vs baseline; "
                     + ("passes every check" if not failed else "fails " + ", ".join(failed)))
    if "ebm" not in risk["results"]:
        lines += ["", "The EBM (`interpret`) candidate was not run: `libebm` ships no linux-aarch64 build, so it cannot "
                  "load in this Docker image on Apple Silicon. Run `python -m ml.benchmark ebm` on an x86-64 host."]
    if dur:
        lines += ["", "## Repair-time models (mean across held-out storms)", "",
                  f"{dur['n_censored']} unrepaired records are used as censored observations by the AFT model only.", "",
                  "| Model | p10–p90 coverage (target 0.80) | Pinball q10 | Pinball q50 | Pinball q90 |", "|---|---|---|---|---|"]
        for name, m in dur["mean"].items():
            lines.append(f"| {name} | {m['coverage']:.2f} | {m['pinball_q10']:.2f} | {m['pinball_q50']:.2f} | "
                         f"{m['pinball_q90']:.2f} |")
    if hazard:
        lines += ["", "## Forecast bridge: peak gust → storm size", "",
                  f"Fitted on ERA5 peak gusts for {hazard['n_storms']} storms and {hazard['n_quiet_days']} quiet days "
                  "(Open-Meteo archive), cross-validated (each storm held out on its own).", "",
                  "| Curve | Poisson deviance | Storm-day log error | Storm-day log bias | Expected reports (actual "
                  f"{hazard['total_actual']:.0f}) |", "|---|---|---|---|---|",
                  f"| fitted (adopted) | {hazard['deviance_fitted']:.0f} | {hazard['storm_mae_log_fitted']:.2f} | "
                  f"{hazard['storm_bias_log_fitted']:+.2f} | {hazard['total_expected_fitted']:.0f} |",
                  f"| previous judgment anchors | {hazard['deviance_previous']:.0f} | {hazard['storm_mae_log_previous']:.2f} | "
                  f"{hazard['storm_bias_log_previous']:+.2f} | {hazard['total_expected_previous']:.0f} |"]
    if fno:
        lines += ["", "## Neural operator (FNO) feasibility test", "", fno["markdown"]]
    (ARTIFACTS / "BENCHMARK.md").write_text("\n".join(lines) + "\n")


def dump_baseline_preds() -> None:
    """Held-out baseline predictions per asset and storm, for the grid-level FNO comparison (research/)."""
    data = pd.read_parquet(ARTIFACTS / "dataset.parquet")
    assets = pd.read_parquet(ARTIFACTS / "assets.parquet")
    arcos = pd.read_parquet(ARTIFACTS / "arcos.parquet")
    storms = pd.read_parquet(ARTIFACTS / "storms.parquet")
    folds = sorted(storms["cv_group"].unique())
    tests = test_sets(assets, arcos, storms, folds)
    out = []
    for g in folds:
        test = tests[g]
        p = calibrated("baseline_lr", data[data["cv_group"] != g]).predict_proba(test)[:, 1]
        out.append(pd.DataFrame({"storm_id": test["storm_id"], "cv_group": g, "x": test["x"], "y": test["y"],
                                 "p": p, "w": test["sample_weight"], "label": test["label"]}))
    pd.concat(out).to_parquet(ARTIFACTS / "bench_baseline_preds.parquet", index=False)


def main() -> None:
    import sys
    if sys.argv[1:] == ["--dump-baseline"]:
        return dump_baseline_preds()
    only = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    t0 = time.time()
    print("Risk models...", flush=True)
    risk = risk_benchmark(only)
    verdict = adoption(risk["results"])
    print("Repair-time models...", flush=True)
    dur = duration_benchmark()
    hazard_path, fno_path = ARTIFACTS / "hazard_curve.json", ARTIFACTS / "fno_results.json"
    hazard = json.loads(hazard_path.read_text()) if hazard_path.exists() else None
    fno = json.loads(fno_path.read_text()) if fno_path.exists() else None
    (ARTIFACTS / "benchmark.json").write_text(json.dumps({"risk": risk, "verdict": verdict, "duration": dur},
                                                         indent=2, default=str))
    write_report(risk, verdict, dur, hazard, fno)
    for name, r in risk["results"].items():
        m = r["mean"]
        print(f"  {name:15s} lift {m['pr_auc_lift']:.2f}x  ROC {m['roc_auc']:.3f}  Brier {m['brier']:.5f}  "
              f"top5% {m['recall_top5pct']:.2f}  " + ("" if name == "baseline_lr" else
                                                      f"adopt={verdict[name]['adopt']}"))
    for name, m in dur["mean"].items():
        print(f"  {name:22s} coverage {m['coverage']:.2f}  pinball q50 {m['pinball_q50']:.2f}")
    print(f"Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
