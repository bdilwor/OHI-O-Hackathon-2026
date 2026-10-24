"""Restoration estimator: labor model, Monte Carlo behaviour, what-if responses."""
import pytest

from restoration import BUNDLES, RestorationEstimator, Scenario

from .constants import DEMO_PREMISE

BASE = {"storm_severity": "high", "crews": 4}


@pytest.fixture(scope="module")
def estimator(scorer, scored_db):
    return RestorationEstimator(scored_db, scorer)


def p50(estimator, **overrides):
    return estimator.estimate(DEMO_PREMISE, {**BASE, **overrides})["p50_hrs"]


def test_every_bundle_cu_exists_in_storms_data(estimator):
    groups = {g for g, _ in estimator.labor.cu_hours}
    for job, variants in BUNDLES.items():
        assert abs(sum(p for p, _, _ in variants) - 1) < 1e-9, job
        for _, _, parts in variants:
            assert {g for g, _, _ in parts} <= groups, job


def test_ilm_from_spreadsheet(estimator):
    assert estimator.labor.ilm == pytest.approx(4.5)


def test_result_shape_and_order(estimator):
    r = estimator.estimate(DEMO_PREMISE, BASE)
    assert r["label"] == "MODEL_ESTIMATE" and not r["fallback"]
    assert 0 <= r["p10_hrs"] <= r["p50_hrs"] <= r["p90_hrs"]
    assert r["bottleneck_device"].startswith("the ")
    assert r["drivers"] and r["assumptions"]


def test_deterministic_with_seed(estimator):
    assert estimator.estimate(DEMO_PREMISE, BASE, seed=7) == estimator.estimate(DEMO_PREMISE, BASE, seed=7)


def test_more_crews_is_faster(estimator):
    assert p50(estimator, crews=8) < p50(estimator, crews=4) < p50(estimator, crews=2)


def test_delay_shifts_estimate(estimator):
    assert p50(estimator, crew_arrival_delay_hrs=12) - p50(estimator) == pytest.approx(12, abs=1.0)


def test_extra_damage_and_blocked_access_slow_it_down(estimator):
    base = p50(estimator)
    assert p50(estimator, extra_damage={"Pole": 3}) > base
    assert p50(estimator, access_blocked=True) > base


def test_severity_ordering(estimator):
    assert p50(estimator, storm_severity="low") <= p50(estimator, storm_severity="medium") <= p50(estimator)


def test_confirmed_damage_is_used(estimator):
    r = estimator.estimate(DEMO_PREMISE, BASE, confirmed={"Pole": 1, "wire": 2})
    assert any("confirmed" in d for d in r["drivers"])


def test_unknown_premise_falls_back(estimator):
    r = estimator.estimate("999", BASE)
    assert r["fallback"] and r["p50_hrs"] is None


@pytest.mark.parametrize("bad", [{"crews": 0}, {"storm_severity": "extreme"}, {"extra_damage": {"Bridge": 1}},
                                 {"crew_arrival_delay_hrs": -1}])
def test_invalid_scenarios_rejected(bad):
    with pytest.raises(ValueError):
        Scenario.from_dict({**BASE, **bad})


def test_no_device_ids_in_output(estimator):
    import json
    import re
    blob = json.dumps(estimator.estimate(DEMO_PREMISE, BASE))
    assert not re.search(r"\d{6,}[A-Z]\d+|CIR\d+|location_n|asset_uid", blob.replace(DEMO_PREMISE, ""))
