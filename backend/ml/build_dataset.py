"""Build the asset-level training set from PostGIS.

    python -m ml.build_dataset

Outputs (backend/ml/artifacts/): assets.parquet, arcos.parquet, storms.parquet,
dataset.parquet (sampled training rows), snap_match_rates.csv.
"""
import json

import pandas as pd

from . import features as F
from .db import ARTIFACTS, get_engine

NEG_RATE_MAJOR = 0.05  # keep 5% of negatives per major storm (weight 20)
NEG_RATE_MINOR = 0.01  # keep 1% of negatives per minor event (weight 100)


def main() -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    engine = get_engine()

    print("Loading assets...")
    assets = F.load_assets(engine)
    print(assets["asset_type"].value_counts().to_string())

    print("Loading ARCOS + clustering storms...")
    arcos = F.load_arcos(engine)
    storms = F.cluster_storms(arcos)
    print(f"  {len(storms)} storms; major: "
          + ", ".join(f"{r.cv_group} ({r.n_records})" for r in storms[storms.cv_group != "minor_events"].itertuples()))

    print("Snapping ARCOS records to assets...")
    arcos["matched_uid"], log = F.snap_labels(arcos, assets)
    print(log.to_string(index=False))
    log.to_csv(ARTIFACTS / "snap_match_rates.csv", index=False)

    print("Building (asset, storm) rows...")
    rows = []
    for s in storms.itertuples():
        rate = NEG_RATE_MINOR if s.cv_group == "minor_events" else NEG_RATE_MAJOR
        rows.append(F.make_rows(assets, arcos, storms, [s.storm_id], neg_rate=rate, seed=s.storm_id))
    data = pd.concat(rows, ignore_index=True)

    n_pairs = len(assets) * len(storms)
    n_pos = int(data["label"].sum())
    print(f"  {len(data):,} rows, {n_pos:,} positives; population positive rate {n_pos / n_pairs:.5%}")

    pd.DataFrame(assets.drop(columns="geom")).to_parquet(ARTIFACTS / "assets.parquet")
    pd.DataFrame(arcos.drop(columns="geom")).to_parquet(ARTIFACTS / "arcos.parquet")
    storms.to_parquet(ARTIFACTS / "storms.parquet")
    data.to_parquet(ARTIFACTS / "dataset.parquet")
    (ARTIFACTS / "dataset_summary.json").write_text(json.dumps({
        "assets": int(len(assets)),
        "storms": int(len(storms)),
        "asset_storm_pairs": int(n_pairs),
        "positives": n_pos,
        "population_positive_rate": n_pos / n_pairs,
        "training_rows": int(len(data)),
        "neg_rate_major": NEG_RATE_MAJOR,
        "neg_rate_minor": NEG_RATE_MINOR,
        "severity_levels": F.severity_levels(storms),
    }, indent=2, default=str))
    print("Done.")


if __name__ == "__main__":
    main()
