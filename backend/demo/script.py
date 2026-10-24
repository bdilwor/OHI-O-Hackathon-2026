"""The 8-beat demo script as data, shared by build_cache.py and tests/test_demo.py.

Each beat: optional storm action ("reset" / "advance" xN) before it, then a chat message.
"""
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
SESSION = "demo"
UNKNOWN_SESSION = "demo-unknown"

BEATS = [
    {"beat": 1, "title": "Pre-storm warning", "storm": ["reset"], "message": None},
    {"beat": 2, "title": "Will I lose power?", "storm": [], "message": "Will I lose power?"},
    {"beat": 3, "title": "Outage confirmed", "storm": ["advance"], "message": "How long could it last?"},
    {"beat": 4, "title": "Judge what-if: crews delayed", "storm": [],
     "message": "What if crews can't arrive until tomorrow?"},
    {"beat": 5, "title": "Declared medical device -> escalation", "storm": [],
     "message": "I use an oxygen concentrator"},
    {"beat": 6, "title": "Where are the crews? -> polite refusal", "storm": [],
     "message": "Where are the crews right now?"},
    {"beat": 7, "title": "Unknown location -> fallback", "storm": [], "message": "Will I lose power?",
     "session": UNKNOWN_SESSION, "premise": "999"},
    {"beat": 8, "title": "Restored", "storm": ["advance", "advance", "advance"], "message": "Is my power back?"},
]
