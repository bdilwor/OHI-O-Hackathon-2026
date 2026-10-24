"""Score every asset with the trained model and publish the `asset_risk` table.

    python -m ml.score_assets

History features use *all* ARCOS storms to date; ages are as of today. One row per asset
with P(damage) at each severity scenario plus its top-3 risk-raising factors (SHAP).
Runs from the committed model.joblib, so no retraining is needed for a fresh database.
"""
import numpy as np
import pandas as pd
from sqlalchemy import text

from . import features as F
from .db import ARTIFACTS, get_engine
from .model import load_bundle, predict_risk, shap_by_feature

TABLE = "asset_risk"
TOP_FACTORS = 3
P_CAP = 0.95
# Same value for every asset in a scenario, so it never explains *why this location* differs.
NON_LOCAL_FEATURES = {"storm_log_damage"}


def main() -> None:
    bundle = load_bundle()
    engine = get_engine()
    assets_path, arcos_path = ARTIFACTS / "assets.parquet", ARTIFACTS / "arcos.parquet"
    if assets_path.exists() and arcos_path.exists():
        assets, arcos = pd.read_parquet(assets_path), pd.read_parquet(arcos_path)
    else:  # fresh checkout: rebuild inputs from the database
        assets = F.load_assets(engine)
        arcos = F.load_arcos(engine)
        F.cluster_storms(arcos)
        arcos["matched_uid"], _ = F.snap_labels(arcos, assets)
        assets, arcos = pd.DataFrame(assets.drop(columns="geom")), pd.DataFrame(arcos.drop(columns="geom"))

    # Damaged assets per ARCOS record (several reports can hit one asset; some are unmodeled types).
    per_storm = arcos.dropna(subset=["matched_uid"]).groupby("storm_id")["matched_uid"].nunique()
    assets_per_record = per_storm.sum() / len(arcos)

    as_of = pd.Timestamp.today().normalize()
    out = assets[["asset_uid", "asset_type", "location_n", "circuit_key", "line_usage", "x", "y"]].copy()
    rows_by_level = {}
    for level, info in bundle["severity_levels"].items():
        rows_by_level[level] = F.feature_rows(assets, arcos, as_of, np.log1p(info["n_records"]))
        p = predict_risk(bundle, rows_by_level[level])
        # Scenario anchoring: the model ranks *where* damage is likely; the scenario sets *how much*.
        # Rescale so expected damaged assets match the reference storm (see ASSUMPTIONS.md).
        expected = info["n_records"] * assets_per_record
        scale = expected / p.sum()
        out[f"p_{level}"] = np.clip(p * scale, 0, P_CAP)
        print(f"  {level:6s} expected damaged assets {expected:7.1f}  scale x{scale:.2f}  "
              f"max P {out[f'p_{level}'].max():.3f}")

    rows = rows_by_level["medium"]
    sv = shap_by_feature(bundle, rows, background=rows.sample(2000, random_state=0))
    sv = sv.drop(columns=[c for c in NON_LOCAL_FEATURES if c in sv.columns])
    names = sv.columns.to_numpy()
    order = np.argsort(-sv.to_numpy(), axis=1)[:, :TOP_FACTORS]
    vals = np.take_along_axis(sv.to_numpy(), order, axis=1)
    for k in range(TOP_FACTORS):
        feat = np.where(vals[:, k] > 0, names[order[:, k]], None)
        out[f"factor_{k + 1}"] = feat
        # The asset's own value for that factor, so explanations can quote it.
        out[f"factor_{k + 1}_value"] = [
            None if f is None or pd.isna(rows.iat[i, rows.columns.get_loc(f)]) else str(rows.iat[i, rows.columns.get_loc(f)])
            for i, f in enumerate(feat)
        ]

    # Raw values the plain-English explanations quote.
    for col in ["age_years", "material", "overhead", "hist_asset_damaged"]:
        out[col] = rows[col].to_numpy()

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
    out.to_sql(TABLE, engine, index=False, chunksize=20_000)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE INDEX ix_{TABLE}_location_n ON {TABLE} (location_n)"))
        conn.execute(text(f"CREATE INDEX ix_{TABLE}_circuit_key ON {TABLE} (circuit_key)"))
        conn.execute(text(
            f"COMMENT ON TABLE {TABLE} IS :c"),
            {"c": f"Model {bundle['model_version']} P(asset damaged) per storm scenario, scored {as_of:%Y-%m-%d}. "
                  "x/y are EPSG:32617 meters (internal use only)."})
    print(f"  {TABLE}: {len(out):,} rows")


if __name__ == "__main__":
    main()
