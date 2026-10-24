"""Did training produce a sane, honest, leak-free model?"""
import json

import numpy as np
import pandas as pd
import pytest

from ml import features as F
from ml.db import ARTIFACTS

# Floors that catch a broken pipeline (random ranking = 1x lift, 0.5 ROC-AUC); not tuning targets.
MIN_PR_AUC_LIFT = 2.0
MIN_ROC_AUC = 0.7
FORBIDDEN_FEATURES = {"critical_c", "esri_defau", "special_op", "premise_nu", "address", "road_name",
                      "anno_strin", "aep_locati", "city"}


@pytest.mark.parametrize("name", ["model.joblib", "features.json", "metrics.json", "METRICS.md",
                                  "calibration.png", "duration.joblib"])
def test_artifact_exists(name):
    assert (ARTIFACTS / name).exists(), f"missing {name}: run `docker compose run --rm ml`"


def test_selected_model_beats_random():
    metrics = json.loads((ARTIFACTS / "metrics.json").read_text())
    mean = metrics["results"][metrics["selected_model"]]["mean"]
    assert mean["pr_auc_lift"] > MIN_PR_AUC_LIFT
    assert mean["roc_auc"] > MIN_ROC_AUC


def test_every_fold_is_scored():
    metrics = json.loads((ARTIFACTS / "metrics.json").read_text())
    for g in metrics["folds"]:
        assert metrics["results"][metrics["selected_model"]][g]["positives"] > 0, g


def test_no_private_or_customer_features(bundle):
    assert not set(bundle["numeric"] + bundle["categorical"]) & FORBIDDEN_FEATURES


def test_severity_levels_increase(bundle):
    levels = bundle["severity_levels"]
    assert levels["low"]["n_records"] < levels["medium"]["n_records"] < levels["high"]["n_records"]


@pytest.fixture(scope="module")
def storm_data(engine):
    arcos = pd.DataFrame(F.load_arcos(engine).drop(columns="geom"))
    storms = F.cluster_storms(arcos)
    arcos["matched_uid"] = pd.NA
    assets = pd.DataFrame({  # a few synthetic assets placed on real damage points
        "asset_uid": [f"test:{i}" for i in range(20)],
        "circuit_key": arcos["circuit_key"].iloc[:20].to_numpy(),
        "x": arcos["x"].iloc[:20].to_numpy(), "y": arcos["y"].iloc[:20].to_numpy(),
        "install_date": pd.Timestamp("2000-01-01"),
    })
    return assets, arcos, storms


def test_first_storm_has_no_history(storm_data):
    assets, arcos, storms = storm_data
    rows = F.make_rows(assets, arcos, storms, [storms["storm_id"].min()], neg_rate=1.0)
    hist = rows[[c for c in F.HISTORY if c != "hist_access_rate_500m"]]
    assert (hist.to_numpy() == 0).all()


def test_history_only_uses_past_storms(storm_data):
    assets, arcos, storms = storm_data
    last = storms["storm_id"].max()
    rows = F.make_rows(assets, arcos, storms, [last], neg_rate=1.0)
    assert rows["hist_share_500m"].max() > 0  # later storms do see history...
    doctored = arcos.copy()  # ...but a "prior" record dated after the storm must be rejected
    doctored.loc[doctored["storm_id"] == 0, "report_tim"] = storms["storm_start"].max() + pd.Timedelta(days=30)
    with pytest.raises(AssertionError, match="history leak"):
        F.make_rows(assets, doctored, storms, [last], neg_rate=1.0)


def test_duration_estimates_are_ordered():
    from ml.duration import estimate_repair_days

    est = estimate_repair_days("Pole", access_issue=True, special_equipment="Backyard Bucket")
    assert 0 <= est["p10_days"] <= est["p50_days"] <= est["p90_days"]
    assert np.isfinite(est["multiplier"]) and est["multiplier"] > 0
