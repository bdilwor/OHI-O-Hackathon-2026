"""Repair-duration helper: p10/p50/p90 repair days -> multiplier for the labor-hour estimator.

    python -m ml.duration

ARCOS dates are day-level, so this is NOT an ETA model. It says how much slower or faster
a damage profile historically was than the typical repair, which the restoration
estimator applies on top of labor hours.
"""
import json

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.metrics import mean_pinball_loss

from .db import ARTIFACTS
from .features import cluster_storms

QUANTILES = (0.1, 0.5, 0.9)
# storm_log_damage is deliberately excluded: with only 35 storms it acts as a storm fingerprint
# and memorizes each storm's repair backlog instead of generalizing (see ASSUMPTIONS.md).
FEATURES = ["asset_type", "access_issue", "special_equipment"]
CATEGORICAL = ["asset_type", "special_equipment"]
BUNDLE_PATH = ARTIFACTS / "duration.joblib"


def prepare(arcos: pd.DataFrame, storms: pd.DataFrame) -> pd.DataFrame:
    df = arcos[(arcos["repaired"] == "Yes") & arcos["repaired_d"].notna()].copy()
    df = df.merge(storms[["storm_id", "cv_group"]], on="storm_id")
    df["repair_days"] = (df["repaired_d"] - df["report_tim"]).dt.days.clip(lower=0)
    df["asset_type"] = df["arcos_asset_type"].fillna("unknown")
    df["access_issue"] = (df["access_iss"] == "Yes").astype(int)
    df["special_equipment"] = df["special_eq"].fillna("none")
    return df


def to_frame(df: pd.DataFrame, categories: dict) -> pd.DataFrame:
    X = df[FEATURES].copy()
    for c in CATEGORICAL:
        X[c] = pd.Categorical(X[c], categories=categories[c])
    return X


def make_model(alpha: float) -> LGBMRegressor:
    # ~1.6k rows: small trees; fit on log1p(days) because repair times are heavily right-skewed.
    return LGBMRegressor(objective="quantile", alpha=alpha, n_estimators=200, learning_rate=0.05,
                         num_leaves=8, min_child_samples=20, random_state=0, verbose=-1)


def main() -> None:
    arcos = pd.read_parquet(ARTIFACTS / "arcos.parquet")
    storms = cluster_storms(arcos)
    df = prepare(arcos, storms)
    categories = {c: sorted(df[c].unique()) for c in CATEGORICAL}
    y = np.log1p(df["repair_days"].to_numpy())
    print(f"{len(df):,} repaired records; median {df['repair_days'].median():.0f} days, "
          f"p90 {df['repair_days'].quantile(0.9):.0f} days")

    # Leave-one-major-storm-out: interval coverage and pinball loss vs. a constant-quantile baseline.
    cv = {}
    for g in sorted(df["cv_group"].unique()):
        tr, te = df["cv_group"] != g, df["cv_group"] == g
        preds = {}
        for q in QUANTILES:
            m = make_model(q).fit(to_frame(df[tr], categories), y[tr])
            preds[q] = np.expm1(m.predict(to_frame(df[te], categories)))
        # Targets are whole days: widen the interval to day boundaries.
        preds[0.1], preds[0.9] = np.floor(preds[0.1]), np.ceil(preds[0.9])
        actual = df.loc[te, "repair_days"].to_numpy()
        cv[g] = {
            "n": int(te.sum()),
            "coverage_p10_p90": float(np.mean((actual >= preds[0.1]) & (actual <= preds[0.9]))),
            **{f"pinball_q{int(q * 100)}": float(mean_pinball_loss(actual, preds[q], alpha=q)) for q in QUANTILES},
            **{f"baseline_pinball_q{int(q * 100)}": float(mean_pinball_loss(
                actual, np.full(len(actual), df.loc[tr, "repair_days"].quantile(q)), alpha=q)) for q in QUANTILES},
        }
        print(f"  {g:15s} n={cv[g]['n']:4d} coverage {cv[g]['coverage_p10_p90']:.2f}  "
              f"pinball q50 {cv[g]['pinball_q50']:.2f} (baseline {cv[g]['baseline_pinball_q50']:.2f})")

    models = {q: make_model(q).fit(to_frame(df, categories), y) for q in QUANTILES}
    bundle = {
        "models": models, "categories": categories,
        "baseline_days": {q: float(df["repair_days"].quantile(q)) for q in QUANTILES},
    }
    joblib.dump(bundle, BUNDLE_PATH, compress=3)
    (ARTIFACTS / "duration_metrics.json").write_text(json.dumps({"cv": cv, "n_records": len(df)}, indent=2))
    for example in [("primary", False, "none"), ("Pole", True, "Backyard Bucket"), ("Transformer", True, "none")]:
        print("  example", example, "->", estimate_repair_days(*example, bundle=bundle))


def estimate_repair_days(asset_type: str, access_issue: bool, special_equipment: str = "none",
                         bundle: dict | None = None) -> dict:
    """Historical repair-day range for a damage profile plus a multiplier vs. the typical repair.

    multiplier = (p50 + 1) / (baseline p50 + 1); +1 avoids dividing by same-day (0-day) repairs.
    """
    bundle = bundle or joblib.load(BUNDLE_PATH)
    row = pd.DataFrame([{
        "asset_type": asset_type if asset_type in bundle["categories"]["asset_type"] else "unknown",
        "access_issue": int(bool(access_issue)),
        "special_equipment": special_equipment if special_equipment in bundle["categories"]["special_equipment"] else "Other",
    }])
    X = to_frame(row, bundle["categories"])
    days = sorted(max(0.0, float(np.expm1(bundle["models"][q].predict(X)[0]))) for q in QUANTILES)
    days[0], days[2] = float(np.floor(days[0])), float(np.ceil(days[2]))  # day-level data
    base = bundle["baseline_days"][0.5]
    return {
        "p10_days": round(days[0], 1), "p50_days": round(days[1], 1), "p90_days": round(days[2], 1),
        "multiplier": round((days[1] + 1) / (base + 1), 2),
        "note": "Day-level historical repair spans (report date to repaired date), not a restoration promise.",
    }


if __name__ == "__main__":
    main()
