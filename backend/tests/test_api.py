"""Tool endpoints, storm timeline and audit, exercised over HTTP before any LLM is involved."""
import json

import pytest

from guidance import SMS_LIMIT

from .constants import DEMO_PREMISE


def test_health(client):
    assert client.get("/health").json()["ok"] is True


def test_lookup_by_address_masks_house_number(client):
    r = client.get("/lookup", params={"address": "7110 Rabbit Run Rd, Roanoke VA"}).json()
    assert r["found"] and r["candidates"][0]["premise"] == DEMO_PREMISE
    assert r["candidates"][0]["masked_address"].startswith("7*** RABBIT RUN")
    assert "7110" not in json.dumps(r)


def test_lookup_by_point(client, scored_db):
    from sqlalchemy import text

    with scored_db.connect() as conn:
        lat, lon = conn.execute(text("SELECT ST_Y(geom), ST_X(geom) FROM customers WHERE premise_nu=:p LIMIT 1"),
                                {"p": DEMO_PREMISE}).one()
    r = client.get("/lookup", params={"lat": lat, "lon": lon}).json()
    assert r["found"] and len(r["candidates"]) == 1


@pytest.mark.parametrize("params", [{"address": "Rabbit Run"}, {}])
def test_lookup_rejects_unusable_input(client, params):
    assert client.get("/lookup", params=params).status_code == 422


def test_lookup_no_match(client):
    r = client.get("/lookup", params={"address": "99999 Nowhere Lane"}).json()
    assert not r["found"] and r["message"]


def test_get_outage_risk(client):
    r = client.post("/tools/get_outage_risk", json={"premise": DEMO_PREMISE, "severity": "high"}).json()
    assert r["risk_band"] in {"Low", "Medium", "High", "Very High"} and "premise" not in r


def test_estimate_restoration_what_if(client, fresh_storm):
    base = client.post("/tools/estimate_restoration", json={"premise": DEMO_PREMISE}).json()
    later = client.post("/tools/estimate_restoration",
                        json={"premise": DEMO_PREMISE, "crew_arrival_delay_hrs": 24}).json()
    assert later["p50_hrs"] > base["p50_hrs"] and later["label"] == "MODEL_ESTIMATE"


def test_estimate_restoration_validation(client):
    assert client.post("/tools/estimate_restoration", json={"premise": DEMO_PREMISE, "crews": 0}).status_code == 422
    r = client.post("/tools/estimate_restoration", json={"premise": DEMO_PREMISE, "extra_damage": {"Bridge": 1}})
    assert r.status_code == 422


def test_prep_guidance_adapts_to_declared_needs(client):
    plain = client.post("/tools/prep_guidance", json={"premise": DEMO_PREMISE}).json()
    medical = client.post("/tools/prep_guidance",
                          json={"premise": DEMO_PREMISE, "declared_needs": ["medical_device"]}).json()
    assert len(medical["actions"]) > len(plain["actions"])
    assert plain["actions"][0]["category"] == "EMERGENCY"
    assert client.post("/tools/prep_guidance",
                       json={"premise": DEMO_PREMISE, "declared_needs": ["guessed_condition"]}).status_code == 422


def test_alert_rules(client, fresh_storm):
    pre = client.post("/tools/check_alert_eligibility", json={"premise": DEMO_PREMISE}).json()
    assert {r["id"] for r in pre["rules_fired"]} == {"R1_PRE_STORM_HIGH_RISK"}
    assert "_internal" not in pre
    client.post("/storm/advance")  # outage
    out = client.post("/tools/check_alert_eligibility",
                      json={"premise": DEMO_PREMISE, "declared_needs": ["medical_device"]}).json()
    ids = {r["id"] for r in out["rules_fired"]}
    assert "R3_OUTAGE_CONFIRMED" in ids and "R5_MEDICAL_LONG_OUTAGE" in ids and out["escalate_to_human"]


@pytest.mark.parametrize("kind", ["pre_storm", "outage_confirmed", "estimate_updated", "restored"])
def test_messages_fit_sms(client, kind):
    m = client.post("/tools/draft_message", json={"premise": DEMO_PREMISE, "kind": kind}).json()
    assert 0 < len(m["text"]) <= SMS_LIMIT


def test_storm_timeline_pushes_proactive_messages(client, fresh_storm):
    steps, kinds = [], []
    for _ in range(4):
        s = client.post("/storm/advance").json()
        steps.append(s["step"])
        kinds.append(s["messages"][-1]["kind"])
    assert steps == ["outage", "damage_assessed", "crews_working", "restored"]
    assert kinds == ["outage_confirmed", "estimate_updated", "estimate_updated", "restored"]
    assert client.post("/storm/advance").json()["step"] == "restored"  # stays at the end
    assert client.get("/storm/messages").json()["messages"][-1]["proactive"] is True


def test_audit_records_calls_with_masked_premise(client):
    client.post("/tools/get_outage_risk", json={"premise": DEMO_PREMISE})
    entry = client.get("/audit", params={"limit": 1}).json()["entries"][0]
    assert entry["tool"] == "get_outage_risk" and entry["input"]["premise"].endswith(DEMO_PREMISE[-4:])
    assert DEMO_PREMISE not in json.dumps(entry)


def test_demo_storm_facts_only_apply_to_demo_home(client, fresh_storm):
    """The scripted outage is for the demo home; other customers must not be told their outage is confirmed."""
    client.post("/storm/advance")  # outage step
    other = "551102222"
    alerts = client.post("/tools/check_alert_eligibility", json={"premise": other}).json()
    assert "R3_OUTAGE_CONFIRMED" not in {r["id"] for r in alerts["rules_fired"]}
    reply = client.post("/chat", json={"message": "How long could it last?", "premise": other,
                                       "session_id": "other-home"}).json()["reply"]
    assert "No outage has been confirmed" in reply and "If your power goes out" in reply
