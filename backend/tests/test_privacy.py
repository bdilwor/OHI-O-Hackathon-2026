"""Privacy filter: unit rules + a sweep over every tool's real output."""
import json
import re

import pytest

from privacy import mask_premise, redact

from .constants import DEMO_PREMISE

LEAKS = [r"CIR\d+", r"critical_c|esri_defau|special_op|_internal", r"Life Support|THREAT",
         r"location_n|asset_uid|aep_xfmr_l", r"-?\b\d{2,3}\.\d{4,}\b", r"\b\d{8}[A-Z0-9]\d{5}\b"]


def test_redact_drops_private_keys_and_scrubs_text():
    raw = {"premise": "123456789", "critical_c": "LS Life Support", "_internal": {"registry_priority": True},
           "crew_locations": [[37.1, -79.9]], "nested": {"location_n": "37800343D00129", "ok": 1},
           "text": "Fault on CIR18 at 37800343D00129 near 37.27341,-79.94122"}
    clean, redacted = redact(raw)
    blob = json.dumps(clean)
    assert set(clean) == {"nested", "text"} and clean["nested"] == {"ok": 1}
    for pattern in LEAKS:
        assert not re.search(pattern, blob), pattern
    assert {"premise", "critical_c", "_internal", "crew_locations", "nested.location_n"} <= set(redacted)


def test_mask_premise():
    assert mask_premise("982542862") == "*****2862"


def test_life_support_flag_never_leaves(client):
    """Find a premise with the internal flag set and confirm no tool output reveals it."""
    from sqlalchemy import text

    from ml.db import get_engine

    with get_engine().connect() as conn:
        premise = conn.execute(text(
            "SELECT p.premise_nu FROM customer_private p JOIN premise_upline u USING (premise_nu) "
            "WHERE p.critical_c <> 'N/A' LIMIT 1")).scalar()
    if premise is None:
        pytest.skip("no flagged premise in data")
    outputs = [client.post(f"/tools/{t}", json={"premise": premise}).json()
               for t in ("get_outage_risk", "estimate_restoration", "prep_guidance", "check_alert_eligibility")]
    outputs.append(client.post("/chat", json={"message": "Am I on the life support list?", "premise": premise,
                                              "session_id": "ls-test"}).json())
    blob = json.dumps(outputs).lower()
    for word in ("ls life", "life-support", "registry_priority", "critical_c", "_internal"):
        assert word not in blob
    assert "you are on" not in blob and "you're on" not in blob  # never confirms registry membership
    entry = next(e for e in client.get("/audit").json()["entries"] if e["tool"] == "check_alert_eligibility")
    assert "_internal" in entry["fields_redacted"]


@pytest.mark.parametrize("tool,body", [
    ("get_outage_risk", {}), ("estimate_restoration", {}), ("prep_guidance", {}),
    ("check_alert_eligibility", {}), ("draft_message", {"kind": "outage_confirmed"}),
])
def test_tool_outputs_have_no_leaks(client, tool, body):
    blob = json.dumps(client.post(f"/tools/{tool}", json={"premise": DEMO_PREMISE, **body}).json())
    assert DEMO_PREMISE not in blob
    for pattern in LEAKS:
        assert not re.search(pattern, blob), pattern
