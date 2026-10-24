"""Privacy filter + audit log. Every tool result passes through redact() before the LLM (or a client) sees it.

Removed: life-support / threat / special-operation flags, anything under an `_internal` key, crew
locations/assignments, internal IDs (asset, device location, circuit), raw coordinates, and the
premise number itself. The internal life-support flag may drive backend escalation, but it is
never returned, confirmed or denied.
"""
import json
import os
import re
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

FORBIDDEN_KEYS = {
    "critical_c", "esri_defau", "special_op", "life_support",
    "location_n", "asset_uid", "aep_xfmr_l", "aep_locati", "upstream_l", "circuit_key", "circuit",
    "x", "y", "lat", "lon", "latitude", "longitude", "geom",
    "crew_location", "crew_locations", "crew_assignments", "crew_ids",
    "premise",
}
FORBIDDEN_PREFIXES = ("_internal", "crew_loc", "crew_assign")
TEXT_SCRUBS = [
    (re.compile(r"\bCIR\d+\b"), "your circuit"),
    (re.compile(r"\b\d{8}[A-Z0-9]\d{5}\b"), "a device on your line"),       # device location ids
    (re.compile(r"-?\b\d{2,3}\.\d{4,}\b"), "[location removed]"),           # lat/lon-like numbers
    (re.compile(r"\b(?:LS )?Life Support\b|\bTHREAT\b", re.I), "[removed]"),
]


def mask_premise(premise) -> str | None:
    if premise is None:
        return None
    s = str(premise)
    return "*" * max(0, len(s) - 4) + s[-4:]


def _scrub(text: str) -> str:
    for pattern, repl in TEXT_SCRUBS:
        text = pattern.sub(repl, text)
    return text


def redact(result, path: str = "") -> tuple[object, list[str]]:
    """Return (clean copy, list of redacted field paths)."""
    redacted: list[str] = []
    if isinstance(result, dict):
        clean = {}
        for k, v in result.items():
            key_path = f"{path}.{k}" if path else k
            if k in FORBIDDEN_KEYS or k.startswith(FORBIDDEN_PREFIXES):
                redacted.append(key_path)
                continue
            clean[k], sub = redact(v, key_path)
            redacted += sub
        return clean, redacted
    if isinstance(result, list):
        out = []
        for i, v in enumerate(result):
            c, sub = redact(v, f"{path}[{i}]")
            out.append(c)
            redacted += sub
        return out, redacted
    if isinstance(result, str):
        scrubbed = _scrub(result)
        if scrubbed != result:
            redacted.append(f"{path} (text scrubbed)")
        return scrubbed, redacted
    return result, redacted


class AuditLog:
    """In-memory ring buffer for the demo panel + append-only JSONL file."""

    def __init__(self, path: str | Path | None = None, maxlen: int = 500):
        self.entries: deque = deque(maxlen=maxlen)
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, tool: str, inputs: dict, clean: object, redacted: list[str], source: str = "api",
               provider: str | None = None) -> dict:
        inputs = {k: (mask_premise(v) if k == "premise" else v) for k, v in (inputs or {}).items()}
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "source": source,
            "provider": provider,
            "input": inputs,
            "fields_returned": sorted(clean.keys()) if isinstance(clean, dict) else [],
            "fields_redacted": redacted,
        }
        with self._lock:
            self.entries.append(entry)
            if self.path:
                with self.path.open("a") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
        return entry

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self.entries)[-limit:][::-1]


def default_audit_log() -> AuditLog:
    return AuditLog(os.environ.get("AUDIT_LOG_PATH", Path(__file__).resolve().parent / "logs" / "audit.jsonl"))
