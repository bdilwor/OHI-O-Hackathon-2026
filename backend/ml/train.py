"""Train and compare outage-damage models with leave-one-major-storm-out CV.

    python -m ml.train

Reads artifacts from build_dataset; writes model.joblib, features.json, metrics.json,
METRICS.md, calibration.png, shap_importance.png.
"""
import json
import time
from datetime import datetime, timezone

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler

from . import features as F
from .db import ARTIFACTS
from .model import BUNDLE_PATH, shap_by_feature

MODEL_VERSION = "outage-risk-v1"
EVAL_NEG_RATE_MINOR = 0.10  # held-out minor events: 10% of negatives (weighted back)
COMPARED = ["logistic_regression", "lightgbm", "random_forest"]
ABLATIONS = ["lightgbm_no_history"]


def feature_lists(name: str) -> tuple[list[str], list[str]]:
    numeric = [f for f in F.NUMERIC if not (name.endswith("no_history") and f in F.HISTORY)]
    return numeric, F.CATEGORICAL


def make_model(name: str) -> Pipeline:
    numeric, categorical = feature_lists(name)
    if name == "logistic_regression":
        prep = ColumnTransformer([
            ("num", Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True)),
                              ("scale", StandardScaler())]), numeric),
            ("cat", OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=50, sparse_output=False),
             categorical),
        ], verbose_feature_names_out=False)
        clf = LogisticRegression(class_weight="balanced", C=0.5, max_iter=3000)
    else:
        ordinal = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1, encoded_missing_value=-1)
        if name == "random_forest":
            num = SimpleImputer(strategy="constant", fill_value=-1, keep_empty_features=True)
            clf = RandomForestClassifier(n_estimators=300, min_samples_leaf=20, max_features="sqrt",
                                         class_weight="balanced_subsample", n_jobs=-1, random_state=0)
        else:  # LightGBM handles NaN natively
            num = "passthrough"
            # Conservative: ~1.5k positives, so shallow trees, large leaves, strong L2.
            clf = LGBMClassifier(n_estimators=300, learning_rate=0.03, num_leaves=15, min_child_samples=200,
                                 subsample=0.8, subsample_freq=1, colsample_bytree=0.7, reg_lambda=5.0,
                                 random_state=0, verbose=-1)
        prep = ColumnTransformer([("num", num, numeric), ("cat", ordinal, categorical)],
                                 verbose_feature_names_out=False)
    return Pipeline([("prep", prep), ("clf", clf)])


def group_splits(groups: pd.Series) -> list[tuple[np.ndarray, np.ndarray]]:
    g = groups.to_numpy()
    return [(np.where(g != k)[0], np.where(g == k)[0]) for k in np.unique(g)]


def fit_calibrated(name: str, data: pd.DataFrame):
    """Fit preprocessing, then sigmoid-calibrate the classifier with storm-grouped CV (weights preserved)."""
    pipe = make_model(name)
    numeric, categorical = feature_lists(name)
    prep, clf = pipe.named_steps["prep"], pipe.named_steps["clf"]
    Xt = prep.fit_transform(data[numeric + categorical])
    cal = CalibratedClassifierCV(clf, method="sigmoid", cv=group_splits(data["cv_group"]), ensemble=False)
    cal.fit(Xt, data["label"].to_numpy(), sample_weight=data["sample_weight"].to_numpy())
    return prep, cal


def topk_recall(y, p, w, frac):
    order = np.argsort(-p)
    in_top = np.cumsum(w[order]) <= frac * w.sum()
    top = order[in_top]
    return float((w[top] * y[top]).sum() / (w * y).sum())


def fold_metrics(y, p, w) -> dict:
    base = float((w * y).sum() / w.sum())
    ap = float(average_precision_score(y, p, sample_weight=w))
    return {
        "positives": int(y.sum()), "base_rate": base, "pr_auc": ap, "pr_auc_lift": ap / base,
        "roc_auc": float(roc_auc_score(y, p, sample_weight=w)),
        "brier": float(brier_score_loss(y, p, sample_weight=w)),
        "recall_top1pct": topk_recall(y, p, w, 0.01), "recall_top5pct": topk_recall(y, p, w, 0.05),
    }


def reliability(y, p, w, bins=10):
    """Weighted reliability curve on quantile bins of p (positives are rare, so equal-width bins are empty)."""
    edges = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
    mean_p, frac_pos = [], []
    for b in range(len(edges) - 1):
        m = idx == b
        if w[m].sum() > 0:
            mean_p.append(np.average(p[m], weights=w[m]))
            frac_pos.append(np.average(y[m], weights=w[m]))
    return np.array(mean_p), np.array(frac_pos)


def main() -> None:
    t0 = time.time()
    data = pd.read_parquet(ARTIFACTS / "dataset.parquet")
    assets = pd.read_parquet(ARTIFACTS / "assets.parquet")
    arcos = pd.read_parquet(ARTIFACTS / "arcos.parquet")
    storms = pd.read_parquet(ARTIFACTS / "storms.parquet")
    summary = json.loads((ARTIFACTS / "dataset_summary.json").read_text())
    folds = sorted(storms["cv_group"].unique())
    print(f"{len(data):,} training rows, folds: {folds}")

    print("Building full held-out evaluation sets...")
    tests = {}
    for g in folds:
        ids = storms.loc[storms["cv_group"] == g, "storm_id"]
        rate = EVAL_NEG_RATE_MINOR if g == "minor_events" else 1.0
        tests[g] = F.make_rows(assets, arcos, storms, ids, neg_rate=rate, seed=1234)
        print(f"  {g}: {len(tests[g]):,} rows, {int(tests[g]['label'].sum())} positives")

    results, oof = {}, {}
    for name in COMPARED + ABLATIONS:
        numeric, categorical = feature_lists(name)
        cols = numeric + categorical
        results[name], parts = {}, []
        for g in folds:
            train = data[data["cv_group"] != g]
            test = tests[g]
            pipe = make_model(name).fit(train[cols], train["label"], clf__sample_weight=train["sample_weight"])
            p = pipe.predict_proba(test[cols])[:, 1]
            y, w = test["label"].to_numpy(), test["sample_weight"].to_numpy()
            results[name][g] = fold_metrics(y, p, w)
            parts.append((y, p, w))
        oof[name] = tuple(np.concatenate(x) for x in zip(*parts))
        mean = {k: float(np.mean([results[name][g][k] for g in folds])) for k in results[name][folds[0]]}
        results[name]["mean"] = mean
        print(f"  {name:22s} PR-AUC lift {mean['pr_auc_lift']:.1f}x  ROC-AUC {mean['roc_auc']:.3f}  "
              f"recall@5% {mean['recall_top5pct']:.2f}  ({time.time() - t0:.0f}s)")

    best = max(COMPARED, key=lambda n: results[n]["mean"]["pr_auc_lift"])
    print(f"Selected: {best}")

    print("Out-of-fold check of the calibrated model...")
    cal_parts, results["calibrated_" + best] = [], {}
    numeric, categorical = feature_lists(best)
    for g in folds:
        prep, cal = fit_calibrated(best, data[data["cv_group"] != g])
        test = tests[g]
        p = cal.predict_proba(prep.transform(test[numeric + categorical]))[:, 1]
        y, w = test["label"].to_numpy(), test["sample_weight"].to_numpy()
        results["calibrated_" + best][g] = fold_metrics(y, p, w)
        cal_parts.append((y, p, w))
    oof["calibrated_" + best] = tuple(np.concatenate(x) for x in zip(*cal_parts))
    results["calibrated_" + best]["mean"] = {
        k: float(np.mean([results["calibrated_" + best][g][k] for g in folds]))
        for k in results["calibrated_" + best][folds[0]]}

    print("Fitting final calibrated model on all storms...")
    prep, cal = fit_calibrated(best, data)
    bundle = {
        "model_version": MODEL_VERSION, "model_name": best, "prep": prep, "model": cal,
        "numeric": numeric, "categorical": categorical,
        "severity_levels": summary["severity_levels"],
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    joblib.dump(bundle, BUNDLE_PATH, compress=3)

    print("SHAP global importance...")
    sample = pd.concat([data[data["label"] == 1], data[data["label"] == 0].sample(4000, random_state=0)])
    sv = shap_by_feature(bundle, sample, background=sample.sample(500, random_state=0))
    importance = sv.abs().mean().sort_values(ascending=False)

    # Calibration curves (out-of-fold)
    fig, ax = plt.subplots(figsize=(6, 6))
    for name, (y, p, w) in oof.items():
        if name in ABLATIONS:
            continue
        mp, fp = reliability(y, p, w)
        ax.plot(mp, fp, marker="o", label=name)
    lim = max(ax.get_xlim()[1], ax.get_ylim()[1])
    ax.plot([0, lim], [0, lim], "k--", lw=1, label="perfect")
    ax.set_xscale("symlog", linthresh=1e-3)
    ax.set_yscale("symlog", linthresh=1e-3)
    ax.set_xlabel("Predicted P(damage)")
    ax.set_ylabel("Observed damage rate")
    ax.set_title("Reliability (out-of-fold, leave-one-major-storm-out)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(ARTIFACTS / "calibration.png", dpi=120)

    fig, ax = plt.subplots(figsize=(7, 6))
    importance.head(15)[::-1].plot.barh(ax=ax)
    ax.set_xlabel("mean |SHAP| (log-odds)")
    ax.set_title(f"Top features ({best})")
    fig.tight_layout()
    fig.savefig(ARTIFACTS / "shap_importance.png", dpi=120)

    (ARTIFACTS / "features.json").write_text(json.dumps({
        "model_version": MODEL_VERSION, "model_name": best, "numeric": numeric, "categorical": categorical,
        "severity_levels": summary["severity_levels"],
        "shap_importance": {k: round(float(v), 4) for k, v in importance.items()},
    }, indent=2))
    (ARTIFACTS / "metrics.json").write_text(json.dumps({
        "selected_model": best, "folds": folds, "results": results, "dataset": summary,
    }, indent=2, default=str))
    write_metrics_md(results, folds, best, importance, summary)
    print(f"Done in {time.time() - t0:.0f}s.")


def write_metrics_md(results, folds, best, importance, summary) -> None:
    def row(name, m):
        return (f"| {name} | {m['pr_auc']:.4f} | {m['pr_auc_lift']:.1f}x | {m['roc_auc']:.3f} | "
                f"{m['brier']:.5f} | {m['recall_top1pct']:.2f} | {m['recall_top5pct']:.2f} |")

    head = ("| Model | PR-AUC | Lift vs base rate | ROC-AUC | Brier | Recall top 1% | Recall top 5% |\n"
            "|---|---|---|---|---|---|---|")
    lines = [
        "# Outage-damage model: metrics",
        "",
        "Validation: **leave-one-major-storm-out**. Each fold holds out one major storm (or all minor",
        "events pooled), trains on the rest, and scores *every* asset in the held-out storm(s).",
        "Damage history features only use storms before the scored storm. Accuracy is not reported:",
        f"with a population positive rate of {summary['population_positive_rate']:.4%}, predicting",
        "\"no damage\" everywhere would be >99.9% accurate and useless.",
        "",
        f"Selected model: **{best}** (highest mean PR-AUC lift), then sigmoid-calibrated.",
        "",
        "## Mean across folds",
        "",
        head,
        *[row(n, results[n]["mean"]) for n in results],
        "",
        "`lightgbm_no_history` is an ablation without damage-history features, to show their contribution.",
        "",
        "## Per fold",
        "",
    ]
    for g in folds:
        any_m = results[best][g]
        lines += [f"### {g} ({any_m['positives']} damaged assets, base rate {any_m['base_rate']:.4%})", "",
                  head, *[row(n, results[n][g]) for n in results], ""]
    lines += ["## Top features (mean |SHAP|)", "", "| Feature | mean abs SHAP |", "|---|---|",
              *[f"| {k} | {v:.3f} |" for k, v in importance.head(12).items()], "",
              "See `calibration.png` and `shap_importance.png`."]
    (ARTIFACTS / "METRICS.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
