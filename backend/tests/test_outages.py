"""Outage details (feed + Outage test tab): location, affected area from topology, why/ETR, privacy, read-only."""
import json
import re
from datetime import datetime

import pytest
from sqlalchemy import text

from .constants import DEMO_PREMISE

OTHER_PREMISE = "551102222"


def outage(client, **params):
    r = client.get("/outage-test", params={"premise": DEMO_PREMISE, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_path_devices_listed(client):
    devices = client.get("/outage-test/devices", params={"premise": DEMO_PREMISE}).json()["devices"]
    assert devices[0]["kind"] == "transformer" and {d["kind"] for d in devices} >= {"fuse", "recloser"}


def test_affected_area_grows_upstream(client):
    devices = client.get("/outage-test/devices", params={"premise": DEMO_PREMISE}).json()["devices"]
    by_kind = {}
    for d in devices:
        by_kind.setdefault(d["kind"], d["index"])
    homes = {k: outage(client, device=i)["affected"]["homes"] for k, i in by_kind.items()}
    assert homes["transformer"] < homes["fuse"] < homes["recloser"]
    assert outage(client, device=by_kind["fuse"])["affects_you"]


def test_record_contents_and_times(client):
    o = outage(client, device=1, cause="tree", reported_hours_ago=3)
    rep = datetime.fromisoformat(o["reported_at"])
    etr = {k: datetime.fromisoformat(v) for k, v in o["etr"].items()}
    assert rep < etr["p10"] <= etr["p50"] <= etr["p90"]
    assert o["hours"]["elapsed"] == pytest.approx(3, abs=0.1)
    assert o["cause"] == "Tree on the power line" and o["location"] and len(o["why"]) >= 3
    assert re.match(r"\w{3}, \d{2} \w{3} \d{4}, \d{1,2}:\d{2} [AP]M", o["reported_display"])  # day month year
    assert o["affected"]["polygon"] and o["affected"]["homes"] >= 1


def test_more_crews_fix_sooner(client):
    slow = outage(client, device=1, cause="pole", crews=1)["hours"]["p50"]
    fast = outage(client, device=1, cause="pole", crews=10)["hours"]["p50"]
    assert fast < slow


def test_map_click_resolves_to_equipment(client):
    devices = client.get("/outage-test/devices", params={"premise": DEMO_PREMISE}).json()["devices"]
    lon, lat = devices[1]["position"]
    o = outage(client, lat=lat, lon=lon, cause="equipment")
    assert o["affected"]["homes"] >= 1
    r = client.get("/outage-test", params={"premise": DEMO_PREMISE, "lat": 0.0, "lon": 0.0})
    assert r.status_code == 422


def test_no_ids_in_payload(client):
    blob = json.dumps(outage(client, device=2))
    for pattern in [r"CIR\d+", r"location_n|asset_uid", r"\b\d{8}[A-Z0-9]\d{5}\b", DEMO_PREMISE]:
        assert not re.search(pattern, blob), pattern


def test_current_outage_feed(client, fresh_storm):
    assert client.get("/outages/current", params={"premise": DEMO_PREMISE}).json()["outage"] is None
    client.post("/storm/advance")  # demo timeline: outage confirmed
    cur = client.get("/outages/current", params={"premise": DEMO_PREMISE}).json()["outage"]
    assert cur["status"] == "in_progress" and cur["source"] == "feed" and cur["etr_display"]["p50"]
    assert client.get("/outages/current", params={"premise": OTHER_PREMISE}).json()["outage"] is None


def test_chat_knows_about_the_test_outage(client, fresh_storm):
    body = {"premise": DEMO_PREMISE, "session_id": "outage-chat",
            "outage": {"device": 1, "cause": "tree", "reported_hours_ago": 2, "crews": 3}}
    r = client.post("/chat", json={**body, "message": "Will I lose power?"}).json()
    assert "power is out" in r["reply"]
    r = client.post("/chat", json={**body, "message": "How long until power is back?"}).json()
    assert r["question_type"] == "restoration" and "most likely be back by" in r["reply"]
    assert r["confidence"]["score"] > 0 and r["what_could_change"]


def test_outage_test_writes_nothing(client, engine):
    def counts():
        with engine.connect() as conn:
            return [conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                    for t in ("customers", "asset_risk", "premise_upline", "arcos_damage")]
    before = counts()
    for i in range(3):
        outage(client, device=i, cause="pole", crews=i + 1)
    assert counts() == before
