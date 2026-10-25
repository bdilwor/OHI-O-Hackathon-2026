"""Reproduce every number in the video deck ("Ahead of the Storm") from the data and the committed model.

    docker compose run --rm ml python -m analysis.deck_numbers        # ~2 min (model fits cached in .cache/)

Needs the loaded database and `python -m ml.build_dataset` (same as quote_check.py). Each line says which
slide the number is on and where it comes from. Uncertainty comes from resampling whole circuits, because
outages cluster: one recloser can take out thousands of homes.
"""
import json
import re
import subprocess
from pathlib import Path

import numpy as np
from sqlalchemy import text

from analysis.quote_check import CACHE, FEATURE_SETS, Data, boot_ratio, ci, cluster_boot, fit_lr, soft_top
from ml import features as F
from ml.db import ARTIFACTS
from ml.train import EVAL_NEG_RATE_MINOR
from restoration import N_RUNS
from sklearn.metrics import average_precision_score

SEED = 20261025
REPS = 2000


def pct(x: float) -> str:
    return f"{x:.1%}"


def main() -> None:
    rng = np.random.default_rng(SEED)
    d = Data()
    metrics = json.loads((ARTIFACTS / "metrics.json").read_text())
    hel, jan, feb = d.majors
    out = lambda slide, what, value, source: print(f"[slide {slide}] {what}: {value}    <- {source}")

    # --- slide 2 ---
    out(2, "biggest storm in our data", d.name(feb), "ARCOS reports clustered into storms (ml/features.py)")

    # --- slide 3: the lookup rule ---
    y, warn = d.out_storms([feb]), d.out_storms([hel])
    caught = int((warn & y).sum())
    out(3, "rule 'out in Helene -> out in Feb 2025'",
        f"{caught:,} of {int(y.sum()):,} homes that lost power = {pct(caught / y.sum())}; rule warned {int(warn.sum()):,}; "
        f"the other {pct(1 - caught / y.sum())} were told 'no'", "home outages traced through premise_upline")
    lab = d.asset_labels(feb)
    rows = d.feature_rows(feb)
    out(3, "Feb 2025 damaged equipment that was damaged in an earlier storm",
        pct(np.mean(rows["hist_asset_damaged"].to_numpy()[lab] > 0)), "ARCOS reports snapped to assets")
    out(3, "Feb 2025 homes out that were out in any earlier storm", pct(d.out_storms(range(0, feb))[y].mean()), "same")

    # --- slide 3: exposure (devices on the home's path to the substation) ---
    codes, counts = cluster_boot(d.home_circuit, REPS, rng)
    lo, hi = (d.path_len <= 4).astype(float), (d.path_len >= 6).astype(float)
    for sid in d.majors:
        yy = d.out_storms([sid]).astype(float)
        r_lo, r_hi = yy[lo == 1].mean(), yy[hi == 1].mean()
        boot = boot_ratio(codes, counts, hi * yy, hi) / boot_ratio(codes, counts, lo * yy, lo)
        c = ci(boot)
        out(3, f"{d.name(sid)} outage rate, 6+ devices vs 4 or fewer",
            f"{pct(r_hi)} vs {pct(r_lo)} = {r_hi / r_lo:.1f}x (95% CI {c[0]:.1f}-{c[1]:.1f}x)", "circuit bootstrap")
    sel = soft_top(d.path_len, int(warn.sum()))
    rec = (sel * y).sum() / y.sum()
    out(3, "same number of warnings as the rule, ranked by device count",
        f"recall {pct(rec)} vs {pct(caught / y.sum())} = {rec / (caught / y.sum()):.2f}x (one storm pair; wide CI)",
        "ties split evenly; see QUOTE_CHECK.md section 2")

    # --- slide 5 ---
    with d.engine.connect() as conn:
        ilm = conn.execute(text("SELECT value FROM labor_indirect_multiplier LIMIT 1")).scalar()
    out(5, "damage reports / storm events / assets scored",
        f"{len(d.arcos):,} / {len(d.storms)} / {len(d.assets):,}", "arcos_damage, storm clustering, asset tables")
    out(5, "labor multiplier / Monte Carlo runs", f"x{ilm:g} / {N_RUNS:,}", "labor workbook (ILM), restoration.py")

    # --- slide 6 ---
    lr = metrics["results"]["calibrated_logistic_regression"]
    folds = metrics["folds"]
    aucs = [lr[g]["roc_auc"] for g in folds]
    top5 = [lr[g]["recall_top5pct"] for g in folds]
    out(6, "ROC-AUC on held-out storms", f"{min(aucs):.3f}-{max(aucs):.3f} (slide: 0.76-0.84)", "ml/artifacts/metrics.json")
    out(6, "damage in the riskiest 5% of equipment", f"{min(top5):.1%}-{max(top5):.1%} per fold, random = 5%",
        "ml/artifacts/metrics.json")
    k = int(round(0.05 * len(d.assets)))
    hist = []
    for sid in d.majors:
        yy = d.asset_labels(sid).astype(float)
        s = soft_top(d.feature_rows(sid)["hist_share_250m"].to_numpy(), k)
        hist.append((s * yy).sum() / yy.sum())
    out(6, "riskiest 5% ranked by nearby past damage only", ", ".join(pct(h) for h in hist), "major storms")
    out(6, "storm dates", f"{d.storms['storm_start'].min():%b %Y} to {d.storms['storm_end'].max():%b %Y}", "ARCOS")

    # --- slide 8 ---
    lifts = {m: metrics["results"][m]["mean"]["pr_auc_lift"] for m in ("logistic_regression", "random_forest", "lightgbm")}
    out(8, "PR-AUC lift (mean over held-out folds)", ", ".join(f"{m} {v:.1f}x" for m, v in lifts.items()),
        "ml/artifacts/metrics.json; 6.9x before shares is documented in ml/ASSUMPTIONS.md")
    nohist = []
    for sid in d.majors:
        yy = d.asset_labels(sid)
        p = d.asset_scores(sid, "structure", "loso")
        nohist.append(average_precision_score(yy, p) / yy.mean())
    ids = d.storms.index[d.storms["cv_group"] == "minor_events"]
    test = F.make_rows(d.assets, d.arcos, d.storms.reset_index(), ids, neg_rate=EVAL_NEG_RATE_MINOR, seed=1234)
    path = CACHE / "p_minor_structure.npy"
    if path.exists():
        p = np.load(path)
    else:
        p = fit_lr(d.train[d.train["cv_group"] != "minor_events"], *FEATURE_SETS["structure"])(test)
        np.save(path, p)
    ym, wm = test["label"].to_numpy(), test["sample_weight"].to_numpy()
    nohist.append(average_precision_score(ym, p, sample_weight=wm) / ((wm * ym).sum() / wm.sum()))
    out(8, "same model with no damage-history inputs", f"lift {np.mean(nohist):.1f}x", "analysis ablation (not deployed)")
    dur = json.loads((ARTIFACTS / "duration_metrics.json").read_text())["cv"]["storm_2025-02"]
    out(8, "repair-time ML vs constant guess (Feb 2025, pinball loss)",
        f"{dur['pinball_q50']:.2f} vs {dur['baseline_pinball_q50']:.2f}", "ml/artifacts/duration_metrics.json")

    # --- slide 9 ---
    res = subprocess.run(["pytest", "--collect-only", "-q", "tests"], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1])
    m = re.search(r"(\d+) tests? collected", res.stdout)
    out(9, "automated tests", m.group(1) if m else "?", "pytest --collect-only")


if __name__ == "__main__":
    main()
