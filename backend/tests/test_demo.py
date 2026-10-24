"""Replay the 8-beat demo through the API (offline assistant) and check each beat's key behaviour."""
from demo.script import BEATS, SESSION

from .constants import DEMO_PREMISE

EXPECT = {
    2: lambda r: "get_outage_risk" in r["tools_used"] and r["question_type"] == "risk",
    3: lambda r: "estimate_restoration" in r["tools_used"] and "outage is confirmed" in r["reply"],
    4: lambda r: "later than before" in r["reply"],
    5: lambda r: "callback" in r["reply"] and "medical_device" in r["declared_needs"],
    6: lambda r: r["tools_used"] == [] and "can't share crew locations" in r["reply"],
    7: lambda r: r["fallback"] and "couldn't find" in r["reply"],
    8: lambda r: "restored" in r["reply"] and r["tools_used"] == [],
}


def test_demo_script_end_to_end(client):
    proactive = []
    for beat in BEATS:
        for action in beat["storm"]:
            state = client.post(f"/storm/{action}").json()
            proactive += state.get("messages", [])
        if beat["beat"] == 1:
            msgs = client.get("/storm/messages").json()["messages"]
            assert msgs[-1]["kind"] == "pre_storm"
            continue
        r = client.post("/chat", json={"message": beat["message"], "session_id": beat.get("session", SESSION),
                                       "premise": beat.get("premise", DEMO_PREMISE)}).json()
        assert EXPECT[beat["beat"]](r), (beat["title"], r["reply"])
    assert [m["kind"] for m in proactive] == ["outage_confirmed", "estimate_updated", "estimate_updated", "restored"]
    client.post("/storm/reset")


def test_demo_safe_mode_serves_cache(client):
    import main

    assistant = main.STATE["assistant"]
    client.post("/storm/reset")
    key = assistant.cache_key("Will I lose power?")
    assistant.cache[key] = {"reply": "cached reply", "tools_used": [], "fallback": False}
    r = client.post("/chat", json={"message": "Will I lose power?", "premise": DEMO_PREMISE, "demo_safe": True,
                                   "session_id": "demo-safe"}).json()
    assistant.cache.pop(key)
    assert r["mode"] == "demo_cache" and r["reply"] == "cached reply"
