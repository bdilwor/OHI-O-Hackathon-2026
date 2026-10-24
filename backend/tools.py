"""The assistant's tools. HTTP endpoints and the LLM both call ToolBox.call(), so every result
is computed the same way, passes through privacy.redact(), and lands in the audit log.
"""
import copy
import json

from sqlalchemy import text

import guidance
from privacy import AuditLog, redact
from restoration import RestorationEstimator
from storm import StormTimeline

MEMO_SIZE = 512
TOOL_NAMES = ("get_outage_risk", "estimate_restoration", "prep_guidance", "check_alert_eligibility", "draft_message")


class ToolBox:
    def __init__(self, engine, scorer, estimator: RestorationEstimator, storm: StormTimeline, audit: AuditLog):
        self.engine = engine
        self.scorer = scorer
        self.estimator = estimator
        self.storm = storm
        self.audit = audit
        self._cache: dict = {}

    # ---- raw tools (unredacted; never return these to a client directly) ----

    def get_outage_risk(self, premise: str, severity: str | None = None) -> dict:
        severity = severity or self.storm.scenario()["storm_severity"]
        return self._memo(("risk", premise, severity), lambda: self.scorer.score(premise, severity))

    def estimate_restoration(self, premise: str, scenario: dict | None = None) -> dict:
        merged = {**self.storm.scenario(), **(scenario or {})}  # what-if fields override the timeline
        return self._memo(("restoration", premise, json.dumps(merged, sort_keys=True)),
                          lambda: self.estimator.estimate(premise, merged, confirmed=self.storm.confirmed_damage(premise)))

    def _memo(self, key: tuple, compute):
        """Results are deterministic per storm step (seeded Monte Carlo), so prep/alerts/messages/chat reuse them."""
        key = (self.storm.step, *key)
        if key not in self._cache:
            if len(self._cache) >= MEMO_SIZE:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = compute()
        return copy.deepcopy(self._cache[key])

    def prep_guidance(self, premise: str, declared_needs: list[str] | None = None) -> dict:
        risk = self.get_outage_risk(premise)
        est = self.estimate_restoration(premise)
        return guidance.prep_guidance(risk.get("risk_band"), est.get("p90_hrs"), declared_needs or [],
                                      outage_active=self.storm.outage_for(premise))

    def check_alert_eligibility(self, premise: str, declared_needs: list[str] | None = None) -> dict:
        needs = guidance.validate_needs(declared_needs)
        risk = self.get_outage_risk(premise)
        est = self.estimate_restoration(premise)
        prev = self.storm.last_p50.get(premise)
        ctx = {
            "step": self.storm.step, "risk_band": risk.get("risk_band"), "needs": needs,
            "outage": self.storm.outage_for(premise), "restored": self.storm.restored_for(premise),
            "hours_out": self.storm.hours_out_for(premise),
            "p50_hrs": est.get("p50_hrs"),
            "etr_slip_hrs": (est["p50_hrs"] - prev) if prev is not None and est.get("p50_hrs") is not None else None,
        }
        return guidance.check_alert_eligibility(ctx, internal_priority=self._registry_flag(premise))

    def draft_message(self, premise: str, kind: str) -> dict:
        risk = self.get_outage_risk(premise) if kind == "pre_storm" else None
        est = self.estimate_restoration(premise) if kind in ("outage_confirmed", "estimate_updated") else None
        return guidance.draft_message(kind, risk, est, previous_p50=self.storm.last_p50.get(premise))

    def _registry_flag(self, premise: str) -> bool:
        """Internal life-support registry flag: backend-only (escalation priority), never returned."""
        with self.engine.connect() as conn:
            v = conn.execute(text("SELECT critical_c FROM customer_private WHERE premise_nu = :p LIMIT 1"),
                             {"p": premise}).scalar()
        return bool(v) and v != "N/A"

    # ---- the only way results leave the backend ----

    def call(self, name: str, args: dict, source: str = "api", provider: str | None = None) -> dict:
        if name not in TOOL_NAMES:
            raise ValueError(f"unknown tool {name}")
        raw = getattr(self, name)(**args)
        clean, redacted = redact(raw)
        self.audit.record(name, args, clean, redacted, source=source, provider=provider)
        return clean
