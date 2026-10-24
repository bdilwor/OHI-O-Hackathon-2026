"""Forecast -> storm size bridge, continuous storm-size interpolation, and the confidence heuristic."""
import numpy as np
import pandas as pd
import pytest

import confidence
from forecast import GUST_ANCHORS, ForecastService, day_storm, gust_to_storm_n, storm_label
from ml.score import p_at

LEVELS = {"low": {"n_records": 12.0}, "medium": {"n_records": 194.0}, "high": {"n_records": 1254.0}}


def test_gust_mapping_hits_anchors_and_is_monotonic():
    for gust, n in GUST_ANCHORS:
        assert gust_to_storm_n(gust) == pytest.approx(n, rel=1e-6)
    values = [gust_to_storm_n(g) for g in range(0, 90, 2)]
    assert all(a <= b for a, b in zip(values, values[1:]))
    assert gust_to_storm_n(120) == GUST_ANCHORS[-1][1]


def test_weather_multipliers():
    base, _ = day_storm(45, 0, 0, 3)
    assert day_storm(45, 0, 0, 66)[0] == pytest.approx(base * 4)       # freezing rain
    assert day_storm(45, 0, 0, 95)[0] == pytest.approx(base * 2)       # thunderstorms
    assert day_storm(45, 2.5, 0, 3)[0] == pytest.approx(base * 1.5)    # heavy rain
    assert "freezing rain" in " ".join(day_storm(45, 0, 0, 66)[1])


def test_labels():
    assert [storm_label(n) for n in (1, 5, 30, 100, 500)] == ["Calm", "Breezy", "Windy", "Storm", "Severe storm"]


def test_fixture_forecast_and_resolve():
    svc = ForecastService(LEVELS)
    fc = svc.forecast(37.18, -80.02)
    assert fc["available"] and len(fc["days"]) == 7 and fc["days"][0]["weekday"] == "Today"
    worst = svc.resolve({"mode": "forecast"}, 37.18, -80.02, pick_worst=True)
    assert worst["day"] == 0 and worst["storm_n"] == pytest.approx(1254.0)
    calm = svc.resolve({"mode": "forecast", "day": 1}, 37.18, -80.02)
    assert calm["label"] == "Calm" and calm["lead_days"] == 1 and calm["when"].startswith("Sun")
    replay = svc.resolve({"mode": "replay", "storm": "feb2025"}, 37.18, -80.02)
    assert replay["hypothetical"] and replay["storm_n"] == 1254.0
    with pytest.raises(ValueError):
        svc.resolve({"mode": "forecast", "day": 9}, 37.18, -80.02)


def test_forecast_outage_degrades_gracefully(monkeypatch):
    svc = ForecastService(LEVELS)
    monkeypatch.setattr(svc, "_fetch", lambda lat, lon: (_ for _ in ()).throw(ConnectionError("offline")))
    st = svc.resolve(None, 37.0, -80.0)
    assert not st["forecast_available"] and st["storm_n"] < 1


def test_p_at_reproduces_anchors_and_is_monotonic():
    df = pd.DataFrame({"p_low": [0.001, 0.0002], "p_medium": [0.01, 0.003], "p_high": [0.05, 0.02]})
    anchors = [(12.0, "p_low"), (194.0, "p_medium"), (1254.0, "p_high")]
    for n, col in anchors:
        assert np.allclose(p_at(df, n, anchors), df[col])
    grid = [p_at(df, n, anchors)[0] for n in (0.5, 5, 12, 50, 194, 600, 1254, 3000)]
    assert all(a <= b for a, b in zip(grid, grid[1:])) and grid[-1] <= 0.95


def test_confidence_penalties_and_missing_list():
    calm_today = {"forecast_available": True, "lead_days": 0}
    best = confidence.assess("risk", circuit_records=124, in_topology=True, storm=calm_today)
    assert best["score"] >= 0.85 and best["label"] == "High" and not best["show_missing"]
    weak = confidence.assess("restoration", circuit_records=2, in_topology=False,
                             storm={"forecast_available": True, "lead_days": 5})
    assert weak["score"] < 0.6 and weak["show_missing"] and 1 <= len(weak["missing"]) <= 3
    assert "past storm damage records for the line that serves you" in weak["missing"]
    no_forecast = confidence.assess("risk", circuit_records=124, in_topology=True, storm={"forecast_available": False})
    assert no_forecast["score"] < best["score"] and "a live weather forecast" in no_forecast["missing"]
