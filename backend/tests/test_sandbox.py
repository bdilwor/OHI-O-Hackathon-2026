"""Sandbox (demo/testing): sliders drive the model, map and chat, and nothing is written anywhere."""
import pytest
from sqlalchemy import text

from .constants import DEMO_PREMISE

BASE = {"mode": "sandbox", "intensity": 3, "crews": 4, "duration_days": 1, "radius_m": 1500}
TABLES = ["customers", "customer_private", "asset_risk", "arcos_damage", "premise_upline", "poles", "transformers"]


def sandbox_map(client, **overrides):
    params = {**BASE, **overrides, "premise": DEMO_PREMISE}
    r = client.get("/map/context", params=params)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def tools(client):
    import main

    return main.STATE["tools"]


def test_intensity_and_duration_raise_storm_size(tools):
    sizes = [tools.sandbox_storm(DEMO_PREMISE, {**BASE, "intensity": i})["storm_n"] for i in range(1, 6)]
    assert sizes == sorted(sizes) and sizes[2] == pytest.approx(194.0)
    longer = tools.sandbox_storm(DEMO_PREMISE, {**BASE, "duration_days": 3})["storm_n"]
    assert longer > sizes[2]


def test_outside_the_storm_circle_is_calm(tools):
    st = tools.sandbox_storm(DEMO_PREMISE, {**BASE, "intensity": 5, "radius_m": 300})
    scorer = tools.scorer
    cx, cy, r, calm = st["area"]
    far = scorer.assets[((scorer.assets["x"] - cx) ** 2 + (scorer.assets["y"] - cy) ** 2) > (r + 50) ** 2].head(200)
    assert len(far) and scorer.p(far, st["storm_n"], st["area"]) == pytest.approx(scorer.p(far, calm))


def test_bigger_area_and_stronger_storm_raise_risk(client):
    small = sandbox_map(client, intensity=4, radius_m=200)["your_risk"]["probability"]
    big = sandbox_map(client, intensity=4, radius_m=3000)["your_risk"]["probability"]
    weak = sandbox_map(client, intensity=1, radius_m=3000)["your_risk"]["probability"]
    assert weak < big and small < big


def test_more_crews_shorter_repairs(client):
    few = sandbox_map(client, intensity=4, crews=2)["restoration"]["p50_hrs"]
    many = sandbox_map(client, intensity=4, crews=20)["restoration"]["p50_hrs"]
    assert many < few


def test_longer_storm_delays_repairs(client):
    one = sandbox_map(client, intensity=4, duration_days=1)["restoration"]["p50_hrs"]
    three = sandbox_map(client, intensity=4, duration_days=3)["restoration"]["p50_hrs"]
    assert three > one + 20  # crews start ~12 h/day later, plus more damage


def test_sandbox_map_payload_is_labeled(client):
    m = sandbox_map(client)
    assert m["storm"]["sandbox"] and m["storm"]["intensity"] == 3 and m["storm"]["radius_m"] == 1500
    assert m["restoration"]["p50_hrs"] is not None and "area" not in m["storm"]


def test_sandbox_chat_reflects_sliders(client):
    calm = client.post("/chat", json={"message": "Will I lose power?", "premise": DEMO_PREMISE,
                                      "session_id": "sandbox-t1", "storm": {**BASE, "intensity": 1}}).json()
    severe = client.post("/chat", json={"message": "Will I lose power?", "premise": DEMO_PREMISE,
                                        "session_id": "sandbox-t2", "storm": {**BASE, "intensity": 5}}).json()
    assert calm["storm"]["sandbox"] and "simulated" in severe["reply"]
    assert severe["storm"]["storm_n"] > calm["storm"]["storm_n"]


def test_invalid_sliders_rejected(client):
    assert client.get("/map/context", params={**BASE, "premise": DEMO_PREMISE, "intensity": 9}).status_code == 422
    r = client.post("/chat", json={"message": "hi", "premise": DEMO_PREMISE, "storm": {**BASE, "crews": 0}})
    assert r.status_code == 422


def test_sandbox_writes_nothing(client, engine):
    def counts():
        with engine.connect() as conn:
            return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES}
    before = counts()
    for i in range(1, 6):
        sandbox_map(client, intensity=i, crews=i * 3, duration_days=i)
        client.post("/chat", json={"message": "How long would repairs take?", "premise": DEMO_PREMISE,
                                   "session_id": "sandbox-w", "storm": {**BASE, "intensity": i}})
    assert counts() == before
    entries = client.get("/audit", params={"limit": 20}).json()["entries"]
    assert any(e["source"] == "sandbox" for e in entries)
