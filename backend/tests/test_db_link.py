"""Is the trained model wired to the database (asset_risk built from, and consistent with, Postgres)?"""
import numpy as np
import pandas as pd
import pytest
from scipy.stats import spearmanr
from sqlalchemy import text

from ml import features as F
from ml.model import predict_risk

SOURCE_TABLES = ["poles", "primary_lines", "secondary_lines", "fuse_banks", "transformers",
                 "recloser_banks", "switches"]


def scalar(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def test_every_source_asset_is_scored(scored_db):
    expected = sum(scalar(scored_db, f"SELECT count(*) FROM {t}") for t in SOURCE_TABLES)
    assert scalar(scored_db, "SELECT count(*) FROM asset_risk") == expected


def test_transformers_join_back_to_gis(scored_db):
    rate = scalar(scored_db, """
        SELECT avg((t.location_n IS NOT NULL)::int) FROM asset_risk r
        LEFT JOIN (SELECT DISTINCT location_n FROM transformers) t USING (location_n)
        WHERE r.asset_type = 'transformer'""")
    assert rate >= 0.99


def test_probabilities_valid_and_ordered(scored_db):
    bad = scalar(scored_db, """
        SELECT count(*) FROM asset_risk
        WHERE NOT (p_low >= 0 AND p_high <= 0.95 AND p_low <= p_medium AND p_medium <= p_high)""")
    assert bad == 0


def test_high_scenario_anchored_to_feb_2025(scored_db, bundle):
    expected = bundle["severity_levels"]["high"]["n_records"] * 0.94
    assert scalar(scored_db, "SELECT sum(p_high) FROM asset_risk") == pytest.approx(expected, rel=0.10)


def test_rescoring_from_db_matches_stored(scored_db, bundle):
    """Rebuild features straight from Postgres, re-predict, and compare to asset_risk."""
    assets = F.load_assets(scored_db)
    arcos = F.load_arcos(scored_db)
    F.cluster_storms(arcos)
    arcos["matched_uid"], _ = F.snap_labels(arcos, assets)
    sample = pd.DataFrame(assets.drop(columns="geom")).sample(500, random_state=0)
    rows = F.feature_rows(sample, pd.DataFrame(arcos.drop(columns="geom")), pd.Timestamp.today().normalize(),
                          np.log1p(bundle["severity_levels"]["medium"]["n_records"]))
    fresh = pd.Series(predict_risk(bundle, rows), index=sample["asset_uid"])
    stored = pd.read_sql(text("SELECT asset_uid, p_medium FROM asset_risk WHERE asset_uid = ANY(:ids)"),
                         scored_db, params={"ids": list(fresh.index)}).set_index("asset_uid")["p_medium"]
    assert len(stored) == len(fresh)
    rho, _ = spearmanr(fresh.loc[stored.index], stored)
    assert rho > 0.99  # same ranking; scale differs only by the scenario-anchoring factor
