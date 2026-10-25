"""Regional estimates for locations outside AEP's data: honest, low-confidence, no AEP IDs."""
import re

import pytest

from regional import make_key, parse_key

from .constants import DEMO_PREMISE

COLUMBUS = (39.961, -82.999)  # Columbus, OH: far outside the challenge area


def risk(client, premise, **storm):
    r = client.post("/tools/get_outage_risk", json={"premise": premise, **({"storm": storm} if storm else {})})
    assert r.status_code == 200, r.text
    return r.json()


def test_locate_point_outside_area(client):
    r = client.get("/regional/locate", params={"lat": COLUMBUS[0], "lon": COLUMBUS[1]}).json()
    assert r["found"] and r["candidate"]["regional"] and r["candidate"]["premise"].startswith("region:")
    assert parse_key(r["candidate"]["premise"])[:2] == pytest.approx(COLUMBUS, abs=0.001)


def test_locate_inside_area_points_to_normal_lookup(client):
    from main import STATE
    lat, lon = STATE["tools"].latlon(DEMO_PREMISE)
    r = client.get("/regional/locate", params={"lat": lat, "lon": lon}).json()
    assert not r["found"] and r["in_aep_area"]


def test_regional_risk_is_low_confidence_and_labeled(client):
    r = risk(client, make_key(*COLUMBUS), mode="forecast", day=0)
    assert r["scope"] == "regional" and not r["fallback"] and r["source"] == "regional_estimate"
    assert r["confidence_meter"]["label"] == "Low" and r["confidence_meter"]["show_missing"]
    assert any("outside AEP" in m for m in r["confidence_meter"]["missing"])
    lo, hi = r["probability_range"]
    assert 0 <= lo <= r["probability"] <= hi <= 1


def test_regional_grows_with_storm_size(client):
    key = make_key(*COLUMBUS)
    calm, severe = risk(client, key, mode="forecast", day=1), risk(client, key, mode="forecast", day=0)
    assert severe["probability"] > calm["probability"]
    est = lambda day: client.post("/tools/estimate_restoration", json={"premise": key, "storm": {
        "mode": "forecast", "day": day}}).json()
    assert est(0)["p50_hrs"] >= est(1)["p50_hrs"] and est(0)["scope"] == "regional"


def test_regional_what_if_crews(client):
    key = make_key(*COLUMBUS)
    few = client.post("/tools/estimate_restoration", json={"premise": key, "crews": 1}).json()
    many = client.post("/tools/estimate_restoration", json={"premise": key, "crews": 8}).json()
    assert many["p50_hrs"] < few["p50_hrs"]


def test_regional_chat_map_and_privacy(client):
    key = make_key(*COLUMBUS, "overhead")
    r = client.post("/chat", json={"message": "Will I lose power?", "session_id": "regional-1", "premise": key}).json()
    assert "outside AEP's data" in r["reply"] and r["confidence"]["label"] == "Low"
    m = client.get("/map/context", params={"premise": key, "day": 0}).json()
    assert m["regional"] and m["homes"][0]["is_you"] and m["poles"] == [] and m["your_risk"]["scope"] == "regional"
    assert not re.search(r"\b\d{9}\b", str(r) + str(m))  # no AEP premise numbers anywhere


def test_bad_regional_key_rejected(client):
    r = client.post("/tools/get_outage_risk", json={"premise": "region:999,0"})
    assert r.status_code == 422


def test_in_area_lookup_unchanged(client):
    r = client.get("/lookup", params={"address": "7110 Rabbit Run, Roanoke"}).json()
    assert r["found"] and r["candidates"][0]["premise"] == DEMO_PREMISE
