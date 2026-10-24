"""Does the chatbot-facing premise scorer behave correctly and safely?"""
import json
import re
import time

from sqlalchemy import text

from ml.score import risk_band

from .conftest import KNOWN_PREMISE

REQUIRED_KEYS = {"premise", "severity", "risk_band", "probability", "confidence", "confidence_reason",
                 "top_factors", "assumptions", "source", "model_version", "fallback"}
LEAK_PATTERNS = [
    r"CIR\d+",                  # circuit names
    r"location_n|asset_uid",    # internal field names
    r"Life Support|THREAT|critical_c|esri_defau",
    r"-?\d{2}\.\d{4,}",         # raw coordinates (probabilities are rounded to 3 decimals)
]


def test_known_premise_schema(scorer):
    r = scorer.score(KNOWN_PREMISE, "medium")
    assert REQUIRED_KEYS <= r.keys()
    assert r["fallback"] is False and r["source"] == "model_estimate"
    assert 0 <= r["probability"] <= 1
    assert r["risk_band"] == risk_band(r["probability"])
    assert r["confidence"] in {"Low", "Medium", "High"}
    assert r["top_factors"] and all(isinstance(f, str) for f in r["top_factors"])


def test_risk_increases_with_severity(scorer):
    p = [scorer.score(KNOWN_PREMISE, s)["probability"] for s in ("low", "medium", "high")]
    assert p[0] < p[1] < p[2]


def test_unknown_premise_falls_back(scorer):
    r = scorer.score("999")
    assert r["fallback"] is True and r["risk_band"] == "Unknown" and r["probability"] is None


def test_map_location(scorer, scored_db):
    with scored_db.connect() as conn:
        lat, lon = conn.execute(text(
            "SELECT ST_Y(geom), ST_X(geom) FROM customers WHERE premise_nu = :p LIMIT 1"),
            {"p": KNOWN_PREMISE}).one()
    r = scorer.score_location(lat, lon)
    assert r["fallback"] is False and r["premise"] is None  # never reveals the snapped premise
    assert scorer.score_location(0.0, 0.0)["fallback"] is True


def test_outputs_leak_nothing_and_are_fast(scorer, sample_premises):
    t0 = time.time()
    results = [scorer.score(p, "high") for p in sample_premises]
    assert time.time() - t0 < 5
    blob = json.dumps([{k: v for k, v in r.items() if k != "premise"} for r in results])
    for pattern in LEAK_PATTERNS:
        assert not re.search(pattern, blob), pattern
