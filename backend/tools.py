"""The assistant's tools. HTTP endpoints and the LLM both call ToolBox.call(), so every result
is computed the same way, passes through privacy.redact(), and lands in the audit log.
"""
import copy
import json

from pyproj import Transformer
from sqlalchemy import text

import confidence
import guidance
from forecast import ForecastService, storm_label
from ml.db import METRIC_CRS
from privacy import AuditLog, redact
from restoration import RestorationEstimator
from storm import StormTimeline

MEMO_SIZE = 512
# Sandbox (demo/testing only): storm category -> expected area damage reports. 1 ~ a typical minor event,
# 3 ~ the Sep 2024 storm, 5 ~ the Feb 2025 storm. Longer storms do more damage; crews start once it passes.
SANDBOX_STORM_N = {1: 12.0, 2: 60.0, 3: 194.0, 4: 600.0, 5: 1254.0}
SANDBOX_NAMES = {1: "Breezy", 2: "Windy", 3: "Strong storm", 4: "Severe storm", 5: "Extreme storm"}
SANDBOX_DURATION_FACTOR = 0.35
SANDBOX_DELAY_HRS_PER_DAY = 12.0
CALM_N = 0.5
SERVICE_AREA_CENTER = (37.27, -79.94)  # forecast location when the premise is unknown
TOOL_NAMES = ("get_outage_risk", "estimate_restoration", "prep_guidance", "check_alert_eligibility", "draft_message",
              "outage_restoration")


class ToolBox:
    def __init__(self, engine, scorer, estimator: RestorationEstimator, storm: StormTimeline, audit: AuditLog,
                 forecast: ForecastService | None = None):
        self.engine = engine
        self.scorer = scorer
        self.estimator = estimator
        self.storm = storm
        self.audit = audit
        self.forecast = forecast or ForecastService(scorer.levels)
        self.outages = None  # OutageService, attached at startup (main.build_state)
        self.to_wgs = Transformer.from_crs(METRIC_CRS, 4326, always_xy=True)
        self._cache: dict = {}

    # ---- storm context: live forecast (default), replay, or a named model scenario ----

    def latlon(self, premise: str | None) -> tuple[float, float]:
        if premise is None or premise not in self.scorer.customers.index:
            return SERVICE_AREA_CENTER
        c = self.scorer.customers.loc[premise]
        lon, lat = self.to_wgs.transform(float(c["x"]), float(c["y"]))
        return lat, lon

    def sandbox_storm(self, premise: str | None, ctx: dict) -> dict:
        """Simulated storm from the sandbox sliders, centered on the home. Read-only: nothing is stored."""
        intensity = int(ctx.get("intensity") or 3)
        days = int(ctx.get("duration_days") or 1)
        radius = float(ctx.get("radius_m") or 1500)
        crews = int(ctx.get("crews") or 4)
        if not (1 <= intensity <= 5 and 1 <= days <= 5 and 100 <= radius <= 5000 and 1 <= crews <= 30):
            raise ValueError("sandbox: intensity 1-5, duration_days 1-5, radius_m 100-5000, crews 1-30")
        n = min(SANDBOX_STORM_N[intensity] * (1 + SANDBOX_DURATION_FACTOR * (days - 1)), 2000.0)
        area = None
        if premise is not None and premise in self.scorer.customers.index:
            c = self.scorer.customers.loc[premise]
            area = (float(c["x"]), float(c["y"]), radius, CALM_N)
        name = SANDBOX_NAMES[intensity]
        return {"mode": "sandbox", "sandbox": True, "hypothetical": True, "storm_n": round(n, 2), "label": name,
                "intensity": intensity, "duration_days": days, "radius_m": radius, "crews": crews,
                "crew_arrival_delay_hrs": SANDBOX_DELAY_HRS_PER_DAY * days, "area": area,
                "lead_days": 0, "forecast_available": True, "date": None,
                "summary": f"a simulated category {intensity} storm ({name.lower()}) lasting {days} day"
                           f"{'s' if days > 1 else ''} across {radius:.0f} m around your home",
                "when": "in this simulated storm",
                "drivers": [f"a simulated category {intensity} storm lasting {days} day{'s' if days > 1 else ''}"]}

    def resolve_storm(self, premise: str | None, storm: dict | None = None, severity: str | None = None) -> dict:
        if severity:  # explicit named scenario (API what-ifs, tests)
            n = self.scorer.storm_size(severity)
            ref = self.scorer.levels[severity]["reference"]
            return {"mode": "scenario", "severity": severity, "storm_n": n, "label": storm_label(n),
                    "hypothetical": True, "lead_days": 0, "forecast_available": True, "date": None,
                    "summary": f"a {severity} storm scenario like {ref}", "when": "in this scenario",
                    "drivers": [f"a {severity} storm scenario like {ref}"]}
        if storm and storm.get("mode") == "sandbox":
            return self.sandbox_storm(premise, storm)
        lat, lon = self.latlon(premise)
        pick_worst = not (storm and storm.get("day") is not None)
        return self.forecast.resolve(storm, lat, lon, pick_worst=pick_worst)

    def _support(self, premise: str) -> dict:
        cust = self.scorer.customers.loc[premise]
        return {"circuit_records": int(self.scorer.circuit_records.get(cust["circuit_key"], 0)),
                "in_topology": self.scorer.graph.has_node(("premise", premise))}

    @staticmethod
    def _storm_public(st: dict) -> dict:
        keys = ("mode", "label", "summary", "when", "date", "storm_n", "lead_days", "hypothetical", "forecast_available",
                "sandbox", "intensity", "duration_days", "radius_m", "crews")
        return {k: st.get(k) for k in keys}

    # ---- raw tools (unredacted; never return these to a client directly) ----

    def get_outage_risk(self, premise: str, severity: str | None = None, storm: dict | None = None) -> dict:
        st = self.resolve_storm(premise, storm, severity)
        risk = self._memo(("risk", premise, st["storm_n"], st["summary"], st.get("area")),
                          lambda: self.scorer.score(premise, None, storm_n=st["storm_n"], area=st.get("area"),
                                                    scenario_note=f"Storm: {st['summary']}."))
        risk["storm"] = self._storm_public(st)
        if not risk.get("fallback"):
            risk["confidence_meter"] = confidence.assess("risk", storm=st, **self._support(premise))
            risk["what_could_change"] = confidence.risk_changers(risk, st)
        return risk

    def estimate_restoration(self, premise: str, scenario: dict | None = None, storm: dict | None = None) -> dict:
        scenario = dict(scenario or {})
        st = self.resolve_storm(premise, storm, scenario.pop("storm_severity", None))
        # What-if fields override the demo timeline's crews/delay (or the sandbox sliders); storm size comes
        # from the storm context. The sandbox never uses the demo timeline's confirmed damage.
        sandbox = st.get("mode") == "sandbox"
        base = ({"crews": st["crews"], "crew_arrival_delay_hrs": st["crew_arrival_delay_hrs"]} if sandbox
                else self.storm.scenario())
        merged = {**base, **scenario, "storm_n": st["storm_n"]}
        damage = None if sandbox else self.storm.confirmed_damage(premise)
        est = self._memo(("restoration", premise, json.dumps(merged, sort_keys=True), st.get("area")),
                         lambda: self.estimator.estimate(premise, merged, confirmed=damage, area=st.get("area")))
        est["storm"] = self._storm_public(st)
        if not est.get("fallback"):
            est["confidence_meter"] = confidence.assess("restoration", storm=st, damage_assessed=bool(damage),
                                                        **self._support(premise))
            est["what_could_change"] = confidence.restoration_changers(est)
        return est

    def _memo(self, key: tuple, compute):
        """Results are deterministic per storm step (seeded Monte Carlo), so prep/alerts/messages/chat reuse them."""
        key = (self.storm.step, *key)
        if key not in self._cache:
            if len(self._cache) >= MEMO_SIZE:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = compute()
        return copy.deepcopy(self._cache[key])

    def outage_restoration(self, premise: str, outage: dict) -> dict:
        """Restoration for a confirmed (test) outage: the fault's damage is known, so this is the repair estimate."""
        rec = self.outages.test(premise, **outage)
        st = {"mode": "outage", "label": "Outage", "summary": "the confirmed outage at your home",
              "when": "for this outage", "hypothetical": False, "forecast_available": True, "lead_days": 0}
        h = rec["hours"]
        est = {"outage_confirmed": True, "cause": rec["cause"], "location": rec["location"],
               "homes_affected": rec["affected"]["homes"], "reported": rec["reported_display"],
               "most_likely_fixed_by": rec["etr_display"]["p50"], "fixed_between": [rec["etr_display"]["p10"],
                                                                                   rec["etr_display"]["p90"]],
               "p10_hrs": h["p10"], "p50_hrs": h["p50"], "p90_hrs": h["p90"], "hours_elapsed": h["elapsed"],
               "bottleneck_device": rec["bottleneck"], "why": rec["why"], "scenario": {"crews": rec["crews"]},
               "storm": st}
        est["confidence_meter"] = confidence.assess("restoration", storm=st, damage_assessed=True,
                                                    **self._support(premise))
        est["what_could_change"] = confidence.restoration_changers(est)
        return est

    def prep_guidance(self, premise: str, declared_needs: list[str] | None = None, storm: dict | None = None) -> dict:
        risk = self.get_outage_risk(premise, storm=storm)
        est = self.estimate_restoration(premise, storm=storm)
        return guidance.prep_guidance(risk.get("risk_band"), est.get("p90_hrs"), declared_needs or [],
                                      outage_active=self.storm.outage_for(premise))

    def check_alert_eligibility(self, premise: str, declared_needs: list[str] | None = None,
                                storm: dict | None = None) -> dict:
        needs = guidance.validate_needs(declared_needs)
        risk = self.get_outage_risk(premise, storm=storm)
        est = self.estimate_restoration(premise, storm=storm)
        prev = self.storm.last_p50.get(premise)
        ctx = {
            "step": self.storm.step, "risk_band": risk.get("risk_band"), "needs": needs,
            "outage": self.storm.outage_for(premise), "restored": self.storm.restored_for(premise),
            "hours_out": self.storm.hours_out_for(premise),
            "p50_hrs": est.get("p50_hrs"),
            "etr_slip_hrs": (est["p50_hrs"] - prev) if prev is not None and est.get("p50_hrs") is not None else None,
        }
        return guidance.check_alert_eligibility(ctx, internal_priority=self._registry_flag(premise))

    def draft_message(self, premise: str, kind: str, storm: dict | None = None) -> dict:
        risk = self.get_outage_risk(premise, storm=storm) if kind == "pre_storm" else None
        est = self.estimate_restoration(premise, storm=storm) if kind in ("outage_confirmed", "estimate_updated") else None
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
