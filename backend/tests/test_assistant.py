"""Assistant: offline routing for the brief's questions, refusals, fallbacks, grounding and the LLM tool loop."""
import json
import os
from types import SimpleNamespace

import pytest

from assistant import MAX_TOOL_CALLS, detect_intents, grounded, parse_what_if

from .constants import DEMO_PREMISE


def chat(client, message, sid, **kw):
    r = client.post("/chat", json={"message": message, "session_id": sid, **kw})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("question,tool", [
    ("Will I lose power?", "get_outage_risk"),
    ("How long could restoration take?", "estimate_restoration"),
    ("What should I do to prepare?", "prep_guidance"),
    ("Do I qualify for outage alerts?", "check_alert_eligibility"),
])
def test_core_questions_use_the_right_tool(client, fresh_storm, question, tool):
    r = chat(client, question, f"core-{tool}", premise=DEMO_PREMISE)
    assert tool in r["tools_used"] and not r["fallback"]
    assert "Confirmed by AEP:" not in r["reply"]  # direct answer, no section headings
    assert 0 < r["confidence"]["score"] <= 1 and r["confidence"]["label"] in {"High", "Medium", "Low"}


@pytest.mark.parametrize("question,qtype", [("Will I lose power?", "risk"),
                                            ("How long could restoration take?", "restoration")])
def test_risk_and_repair_answers_list_what_could_change(client, fresh_storm, question, qtype):
    r = chat(client, question, f"changers-{qtype}", premise=DEMO_PREMISE)
    assert r["question_type"] == qtype and 2 <= len(r["what_could_change"]) <= 3


def test_answer_numbers_come_from_the_model(client, fresh_storm):
    r = chat(client, "Will I lose power?", "numbers", premise=DEMO_PREMISE)
    risk = client.post("/tools/get_outage_risk", json={"premise": DEMO_PREMISE}).json()
    assert f"{risk['probability'] * 100:.0f}%" in r["reply"] and r["storm"]["label"] == risk["storm"]["label"]


def test_storm_setting_from_the_map_drives_the_answer(client, fresh_storm):
    calm = chat(client, "Will I lose power?", "calm-day", premise=DEMO_PREMISE, storm={"mode": "forecast", "day": 1})
    stormy = chat(client, "Will I lose power?", "stormy-day", premise=DEMO_PREMISE, storm={"mode": "forecast", "day": 0})
    assert "low" in calm["reply"] and calm["storm"]["label"] == "Calm"
    assert stormy["storm"]["storm_n"] > calm["storm"]["storm_n"]


def test_low_confidence_lists_missing_data(client, fresh_storm):
    r = chat(client, "How long could it last?", "far-day", premise=DEMO_PREMISE, storm={"mode": "forecast", "day": 6})
    assert r["confidence"]["score"] < 0.6 and r["confidence"]["show_missing"]
    assert 2 <= len(r["confidence"]["missing"]) <= 3


def test_day_named_in_question(client, fresh_storm):
    r = chat(client, "Will I lose power tomorrow?", "tomorrow", premise=DEMO_PREMISE)
    assert r["question_type"] == "risk" and r["storm"]["lead_days"] == 1


def test_what_if_reports_change(client, fresh_storm):
    sid = "whatif"
    chat(client, "How long could it last?", sid, premise=DEMO_PREMISE)
    r = chat(client, "What if crews can't arrive until tomorrow?", sid)
    assert r["question_type"] == "restoration" and "later than before" in r["reply"]


def test_declared_medical_need_escalates_without_promises(client, fresh_storm):
    client.post("/storm/advance")  # outage confirmed
    r = chat(client, "I use an oxygen concentrator", "medical", premise=DEMO_PREMISE)
    assert "medical_device" in r["declared_needs"]
    assert "callback" in r["reply"] and "can't schedule it myself" in r["reply"]
    assert "dispatched" not in r["reply"].lower()


@pytest.mark.parametrize("question", ["Where are the crews right now?", "Which truck is coming to my street?",
                                      "Is my neighbor's power out?", "Is the hospital without power?"])
def test_refusals(client, question):
    r = chat(client, question, "refuse", premise=DEMO_PREMISE)
    assert r["tools_used"] == [] and "can't share" in r["reply"]


def test_no_location_and_unknown_location_fall_back(client):
    r = chat(client, "Will I lose power?", "nolocation")
    assert r["fallback"] and "35 feet" in r["reply"]
    r = chat(client, "Will I lose power?", "unknown", premise="999")
    assert r["fallback"] and "couldn't find" in r["reply"] and "911" in r["reply"]
    assert r["confidence"]["score"] < 0.6 and r["confidence"]["missing"]


def test_intent_and_what_if_parsing():
    assert detect_intents("Is power on yet?") != ["refuse_others"]
    assert parse_what_if("what if crews can't arrive until tomorrow", {"crews": 4}) == {"crew_arrival_delay_hrs": 24.0}
    assert parse_what_if("what if there were 3 broken poles and 8 crews", {})["extra_damage"] == {"Pole": 3}


def test_grounding_check():
    tool_result = {"p10_hrs": 8, "p50_hrs": 17, "p90_hrs": 31.5, "probability": 0.558}
    assert grounded("Likely 17 hours (8-31.5), about 56% risk.", [tool_result])[0]
    ok, bad = grounded("Power will be back in 4 hours.", [tool_result])
    assert not ok and bad == [4.0]


# ---- LLM path with a scripted fake provider (no network) ----

def _tool_call(name, args, i):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(id=f"call_{i}", function=fn,
                           model_dump=lambda: {"id": f"call_{i}", "type": "function",
                                               "function": {"name": name, "arguments": json.dumps(args)}})


@pytest.fixture
def fake_llm(client, monkeypatch):
    import main

    assistant = main.STATE["assistant"]
    monkeypatch.setattr(assistant, "_providers", lambda: ["fake"])
    script = []

    def call_llm(messages, tools, providers):
        assert all("982542862" not in json.dumps(m, default=str) for m in messages), "premise leaked to LLM"
        return script.pop(0), "fake"

    monkeypatch.setattr(assistant, "call_llm", call_llm)
    return script


def test_llm_tool_loop_and_grounded_reply(client, fresh_storm, fake_llm):
    risk = client.post("/tools/get_outage_risk", json={"premise": DEMO_PREMISE}).json()
    pct = round(risk["probability"] * 100)
    fake_llm += [
        SimpleNamespace(content="", tool_calls=[_tool_call("get_outage_risk", {}, 1)]),
        SimpleNamespace(content=f"Your outage risk is {risk['risk_band']}: about {pct}%.", tool_calls=None),
    ]
    r = chat(client, "Will I lose power?", "llm-ok", premise=DEMO_PREMISE)
    # The backend pre-runs the model for core questions, then the LLM's own tool call runs too.
    assert r["mode"] == "llm" and r["tools_used"] == ["get_outage_risk", "get_outage_risk"] and f"{pct}%" in r["reply"]
    assert r["question_type"] == "risk" and r["what_could_change"] and r["confidence"]["score"] > 0


def test_llm_invented_number_is_replaced(client, fresh_storm, fake_llm):
    fake_llm += [SimpleNamespace(content="Model estimate: power back in exactly 3.7 hours, guaranteed.",
                                 tool_calls=None)]
    r = chat(client, "How long could it last?", "llm-bad", premise=DEMO_PREMISE)
    assert r["mode"] == "offline" and r["grounding_failed"] and "3.7" not in r["reply"]


def test_llm_tool_budget(client, fresh_storm, fake_llm):
    fake_llm += [SimpleNamespace(content="", tool_calls=[_tool_call("get_outage_risk", {}, i)])
                 for i in range(MAX_TOOL_CALLS)]
    fake_llm += [SimpleNamespace(content="Nothing new to report.", tool_calls=None)]
    r = chat(client, "Will I lose power?", "llm-budget", premise=DEMO_PREMISE)
    assert len(r["tools_used"]) == MAX_TOOL_CALLS


@pytest.mark.skipif(not os.environ.get("GROQ_API_KEY"), reason="live LLM smoke test needs GROQ_API_KEY")
def test_live_llm_smoke(client, fresh_storm, monkeypatch):
    import main

    assistant = main.STATE["assistant"]
    monkeypatch.setattr(assistant, "provider", "groq")
    # One retry for transient provider issues (free-tier 429s, network). The reason is reported if both fail.
    for attempt in range(2):
        r = chat(client, "Will I lose power?", f"live-{attempt}", premise=DEMO_PREMISE)
        if r["mode"] == "llm":
            break
    if r["mode"] != "llm" and "RateLimitError" in (r.get("note") or ""):
        pytest.skip("LLM provider rate limit reached (free-tier tokens/minute); not a code failure")
    assert r["mode"] == "llm", r.get("note")  # an offline fallback must not pass as a live answer
    assert r["tools_used"] and r["reply"]


def test_grounding_reads_unicode_fractions():
    assert grounded("About 1½ hours.", [{"p50_hrs": 1.5}])[0]
    ok, bad = grounded("About 2½ hours.", [{"p50_hrs": 1.5}])
    assert not ok and bad == [2.5]
