"""LLM middleware: tool-calling loop, grounding check, deterministic answer card, offline/demo-safe modes.

Providers (env LLM_PROVIDER): groq (default, falls back to gemini), gemini, offline.
Both cloud providers expose OpenAI-compatible endpoints, so one SDK covers them. Keys come only
from env (GROQ_API_KEY, GEMINI_API_KEY). With no key, or in tests, the offline router answers:
a deterministic keyword -> tools -> template pipeline that uses the same tools and rules.

The LLM never sees the premise number or any private field: tools are called with the session's
premise injected server-side, and results pass through privacy.redact() first.
"""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from guidance import DECLARED_NEEDS

MAX_TOOL_CALLS = 5
TEMPERATURE = 0.2
HISTORY_TURNS = 6
CACHE_PATH = Path(__file__).resolve().parent / "demo" / "cache.json"

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-120b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "GEMINI_MODEL",
               "gemini-2.0-flash"),
}

SYSTEM_PROMPT = """You are AEP's AI Outage Concierge, helping one residential customer with a storm outage.

Rules you must always follow:
- Never state a number, time, percentage or probability that did not come from a tool result in this conversation. If you need a number, call a tool.
- Always separate three sections, using these exact headings: "Confirmed by AEP:", "Model estimate:", "What you can do:". Only facts from the storm status are confirmed; risk and restoration numbers are model estimates.
- Give ranges, not single restoration times, and say what drives the uncertainty.
- Politely refuse requests for crew locations or assignments, other customers' information, or critical-facility details, and briefly explain why (safety and privacy). Never confirm or deny whether anyone is on a medical or life-support registry.
- If a tool cannot estimate (unknown location, missing data), say so plainly and still give the safety guidance.
- Never claim to have dispatched a crew, scheduled a call, or made any commitment on AEP's behalf. Say what the alert rules recommend or what the customer "qualifies for", never that something "will" happen.
- Only say the power is on or off if the confirmed facts say so.
- For "what if" questions, call estimate_restoration with the changed scenario, and say how many hours later or sooner the most likely time is than before (from change_vs_previous).
- Special needs come only from what the customer tells you; never guess them.
- Don't give phone numbers, web addresses or opening hours; say "report it to AEP". For danger, say "call 911".
- Quote hours exactly as the tool gives them (or rounded to whole hours). Say "most likely" and "likely range"; don't mention percentiles.
- Use plain language. Keep replies under 120 words; they may be sent as SMS.
"""

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_outage_risk",
        "description": "Outage risk at the customer's home for the current storm: risk band, probability, confidence, top factors.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "estimate_restoration",
        "description": "Restoration time range (P10/P50/P90 hours) if/while the customer's power is out. Pass fields only to explore what-if scenarios.",
        "parameters": {"type": "object", "properties": {
            # Nullable: some models send null for every optional field, which strict validators reject otherwise.
            "storm_severity": {"type": ["string", "null"], "enum": ["low", "medium", "high", None]},
            "crews": {"type": ["integer", "null"], "minimum": 1, "maximum": 50},
            "crew_arrival_delay_hrs": {"type": ["number", "null"], "minimum": 0, "maximum": 240},
            "extra_damage": {"type": ["object", "null"], "additionalProperties": {"type": "integer"},
                             "description": "Extra damage on the customer's line, e.g. {\"Pole\": 3, \"wire\": 1}"},
            "access_blocked": {"type": ["boolean", "null"]}}}}},
    {"type": "function", "function": {
        "name": "prep_guidance",
        "description": "Safety and preparation actions tailored to the risk, expected duration and the customer's declared needs.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "check_alert_eligibility",
        "description": "Which outage alert/escalation rules apply to this customer right now, with channel and timing.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "draft_message",
        "description": "Draft a short customer SMS.",
        "parameters": {"type": "object", "required": ["kind"], "properties": {
            "kind": {"type": "string", "enum": ["pre_storm", "outage_confirmed", "estimate_updated", "restored"]}}}}},
]

NEED_KEYWORDS = {
    "medical_device": ["oxygen", "concentrator", "cpap", "bipap", "ventilator", "dialysis", "nebulizer",
                       "medical device", "medical equipment", "home medical"],
    "refrigerated_meds": ["insulin", "refrigerated med", "medicine in the fridge", "refrigerate my med"],
    "well_pump": ["well pump", "well water"],
    "sump_pump": ["sump pump", "sump"],
    "mobility": ["wheelchair", "stair lift", "stairlift", "mobility"],
}
INTENTS = {
    "refuse_crews": [r"where (are|is) (the )?crew", r"crew (location|position|assignment)", r"which (truck|crew)",
                     r"where.*(truck|lineman|linemen)"],
    "refuse_others": [r"neighbou?r", r"other customer", r"someone else", r"who else", r"critical facilit",
                      r"(where|which|status|power|location).*(hospital|fire station|ambulance)",
                      r"(hospital|fire station|ambulance).*(power|location|where|out)",
                      r"registry|registered|life.?support (list|customer|status)"],
    "what_if": [r"what if", r"suppose", r"delayed?", r"tomorrow", r"can'?t (get|arrive|reach)", r"more crews",
                r"fewer crews", r"road.*(blocked|closed)", r"blocked"],
    "restoration": [r"how long", r"restor", r"when will", r"back on", r"\betr\b", r"last\b", r"get power back"],
    "risk": [r"lose power", r"outage risk", r"chance", r"likel", r"will (i|my|we)", r"risk"],
    "prep": [r"prepar", r"what should i do", r"get ready", r"\bsafe", r"tips", r"\bplan\b"],
    "alerts": [r"alert", r"notif", r"text me", r"qualif", r"sign up", r"call me"],
}


@dataclass
class Session:
    premise: str | None = None
    needs: list = field(default_factory=list)
    last_estimate: dict | None = None
    last_results: dict = field(default_factory=dict)
    history: list = field(default_factory=list)


def detect_needs(message: str) -> list[str]:
    m = message.lower()
    return [need for need, words in NEED_KEYWORDS.items() if any(w in m for w in words)]


def detect_intents(message: str) -> list[str]:
    m = message.lower()
    found = [name for name, pats in INTENTS.items() if any(re.search(p, m) for p in pats)]
    if "what_if" in found:  # a what-if is a restoration question with changed inputs
        found = [i for i in found if i not in ("restoration", "risk")]
    return found


def parse_what_if(message: str, base: dict) -> dict:
    """Scenario overrides from a what-if question, relative to the current storm scenario."""
    m = message.lower()
    out = {}
    hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:more\s+)?(?:hours?|hrs?)", m)
    if "tomorrow" in m:
        out["crew_arrival_delay_hrs"] = 24.0
    elif hours and re.search(r"delay|late|arriv|can'?t|wait", m):
        out["crew_arrival_delay_hrs"] = float(hours.group(1))
    elif re.search(r"delay|late|can'?t (get|arrive|reach)", m):
        out["crew_arrival_delay_hrs"] = base.get("crew_arrival_delay_hrs", 0) + 12.0
    crews = re.search(r"(\d+)\s*crews?", m)
    if crews:
        out["crews"] = int(crews.group(1))
    elif "more crews" in m:
        out["crews"] = base.get("crews", 2) * 2
    elif "fewer crews" in m:
        out["crews"] = max(1, base.get("crews", 2) // 2)
    poles = re.search(r"(\d+)\s*(?:more\s+)?(?:broken\s+|damaged\s+)?poles?", m)
    if poles:
        out["extra_damage"] = {"Pole": int(poles.group(1))}
    if re.search(r"block|closed|flood|tree.*road|can'?t reach", m):
        out["access_blocked"] = True
    for sev in ("low", "medium", "high"):
        if f"{sev} storm" in m or f"{sev} severity" in m:
            out["storm_severity"] = sev
    return out


SAFE_NUMBERS = {911, 35}  # emergency number and downed-line distance from the safety rules
LLM_DROP_KEYS = {"rules_table", "assumptions", "source", "model_version", "scenario", "chars", "channel",
                 "opt_in_available", "label"}
LLM_MAX_ITEMS = 6


def compact_for_llm(result):
    """Smaller tool payloads for the LLM (free-tier token limits). Cards and audit keep the full result."""
    if isinstance(result, dict):
        return {k: compact_for_llm(v) for k, v in result.items() if k not in LLM_DROP_KEYS and v is not None}
    if isinstance(result, list):
        return [compact_for_llm(v) for v in result[:LLM_MAX_ITEMS]]
    return result


def clean_tool_args(args: dict) -> dict:
    """Drop nulls/no-op values models fill in for optional fields, so they aren't treated as what-ifs."""
    return {k: v for k, v in (args or {}).items()
            if v is not None and v != {} and not (k == "access_blocked" and v is False)}


def numbers_in(obj) -> set[float]:
    found = set()
    for n in re.findall(r"(?<![A-Za-z\d.])-?\d+(?:\.\d+)?", json.dumps(obj) if not isinstance(obj, str) else obj):
        try:
            v = float(n)
        except ValueError:
            continue
        found.add(v)
        if 0 < abs(v) <= 1:  # probabilities may be quoted as percentages
            found.update({round(v * 100), round(v * 100, 1)})
    return found


def grounded(reply: str, sources: list) -> tuple[bool, list[float]]:
    """Every number in the reply must come from tool results / storm state / the user's own message."""
    allowed = set().union(*(numbers_in(s) for s in sources)) if sources else set()
    allowed |= {0, 1, 2, 3} | SAFE_NUMBERS  # small counting words are not claims; safety constants are rules
    def ok(n: float) -> bool:  # exact (to 1 decimal), or a whole-number rounding of a grounded value
        return any(abs(n - a) <= 0.051 or (n.is_integer() and abs(n - a) <= 0.5) for a in allowed)
    bad = [n for n in numbers_in(reply) if not ok(n)]
    return not bad, bad


class Assistant:
    def __init__(self, toolbox, storm):
        self.tools = toolbox
        self.storm = storm
        self.sessions: dict[str, Session] = {}
        self.provider = os.environ.get("LLM_PROVIDER", "groq").lower()
        self.cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}

    # ---- public ----

    def chat(self, message: str, session_id: str = "default", premise: str | None = None,
             declared_needs: list[str] | None = None, demo_safe: bool = False) -> dict:
        s = self.sessions.setdefault(session_id, Session())
        if premise:
            s.premise = str(premise)
        if declared_needs is not None:
            s.needs = [n for n in declared_needs if n in DECLARED_NEEDS]
        newly_declared = [n for n in detect_needs(message) if n not in s.needs]
        s.needs += newly_declared

        if demo_safe:
            cached = self.cache.get(self.cache_key(message))
            if cached:
                return {**cached, "mode": "demo_cache"}
        providers = self._providers()
        if providers:
            try:
                return self._llm_turn(s, message, providers, newly_declared)
            except Exception as e:  # provider down / quota / network: stay useful
                result = self._offline_turn(s, message, newly_declared)
                result["note"] = f"LLM unavailable ({e.__class__.__name__}); answered with the rules-based assistant."
                return result
        return self._offline_turn(s, message, newly_declared)

    def cache_key(self, message: str) -> str:
        return f"{self.storm.step}|{re.sub(r'[^a-z0-9 ]', '', message.lower()).strip()}"

    # ---- providers ----

    def _providers(self) -> list[str]:
        if self.provider == "offline":
            return []
        order = [self.provider] + [p for p in PROVIDERS if p != self.provider]
        return [p for p in order if p in PROVIDERS and os.environ.get(PROVIDERS[p][1])]

    def call_llm(self, messages: list, tools: list | None, providers: list[str]):
        from openai import OpenAI

        last_error = None
        for name in providers:
            base_url, key_env, model_env, default_model = PROVIDERS[name]
            # max_retries backs off on 429 rate limits (free tiers) using the server's retry-after.
            client = OpenAI(base_url=base_url, api_key=os.environ[key_env], timeout=30, max_retries=3)
            try:
                kwargs = {"model": os.environ.get(model_env, default_model), "messages": messages,
                          "temperature": TEMPERATURE}
                if tools:
                    kwargs.update(tools=tools, tool_choice="auto")
                return client.chat.completions.create(**kwargs).choices[0].message, name
            except Exception as e:
                last_error = e
        raise last_error or RuntimeError("no LLM provider configured")

    # ---- tool execution (premise + needs injected server-side) ----

    def _run_tool(self, s: Session, name: str, args: dict, provider: str | None) -> dict:
        args = clean_tool_args(args)
        if name == "get_outage_risk":
            args = {}  # always the current storm scenario
        if name == "estimate_restoration":
            call_args = {"premise": s.premise, "scenario": args}
        elif name in ("prep_guidance", "check_alert_eligibility"):
            call_args = {"premise": s.premise, "declared_needs": s.needs}
        else:
            call_args = {"premise": s.premise, **args}
        result = self.tools.call(name, call_args, source="chat", provider=provider)
        if name == "estimate_restoration" and result.get("p50_hrs") is not None:
            prev = s.last_estimate
            if prev and args:  # what-if: report the change vs. the previous estimate
                result["change_vs_previous"] = {
                    "previous_p50_hrs": prev["p50_hrs"], "previous_p90_hrs": prev["p90_hrs"],
                    "p50_change_hrs": round(result["p50_hrs"] - prev["p50_hrs"], 1),
                    "p90_change_hrs": round(result["p90_hrs"] - prev["p90_hrs"], 1),
                }
            if not args or prev is None:  # what-ifs don't replace the baseline estimate
                s.last_estimate = result
        s.last_results[name] = result
        return result

    # ---- LLM turn ----

    def _context_notes(self, s: Session) -> list[dict]:
        facts = " ".join(self._confirmed_lines(self.storm.confirmed_for(s.premise)))
        needs = "; ".join("they " + DECLARED_NEEDS[n] for n in s.needs) or "none"
        return [
            {"role": "system", "content": f"Confirmed by AEP right now (copy these facts exactly; nothing else is "
                                          f"confirmed): {facts}"},
            {"role": "system", "content": f"Background for you, not to list as confirmed facts: the customer's "
                                          f"home is identified. What the customer told you about their needs: "
                                          f"{needs}."},
        ]

    def _llm_turn(self, s: Session, message: str, providers: list[str], newly_declared: list[str]) -> dict:
        if not s.premise:
            return self._offline_turn(s, message, newly_declared)
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    *self._context_notes(s),
                    *s.history[-2 * HISTORY_TURNS:],
                    {"role": "user", "content": message}]
        tools_used, results, provider = [], [], None
        if newly_declared:  # a newly declared need always gets tailored guidance + the alert/escalation rules
            for name in ("prep_guidance", "check_alert_eligibility"):
                out = self._run_tool(s, name, {}, None)
                tools_used.append(name)
                results.append(out)
            messages.insert(-1, {"role": "system", "content": (
                "The customer just declared a need. Tool results already retrieved (use them; mention any "
                "escalation the alert rules recommend, without promising it): "
                + json.dumps({"prep_guidance": compact_for_llm(results[0]),
                              "check_alert_eligibility": compact_for_llm(results[1])}))})
        reply = None
        while True:
            budget_left = MAX_TOOL_CALLS - len(tools_used)
            msg, provider = self.call_llm(messages, TOOL_SPECS if budget_left > 0 else None, providers)
            if not getattr(msg, "tool_calls", None):
                reply = (msg.content or "").strip()
                break
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                if len(tools_used) >= MAX_TOOL_CALLS:
                    out = {"error": "tool call limit reached for this turn"}
                else:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                        out = self._run_tool(s, tc.function.name, args, provider)
                        tools_used.append(tc.function.name)
                        results.append(out)
                    except Exception as e:
                        out = {"error": f"tool failed: {e}"}
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(compact_for_llm(out))})

        ok, bad = grounded(reply, results + [self.storm.confirmed_for(s.premise), message]
                           + list(s.last_results.values()))
        escalation = next((r for r in results if isinstance(r, dict) and r.get("escalate_to_human")), None)
        if ok and escalation and "callback" not in reply.lower():  # safety-relevant: never left to LLM variance
            reply += ("\n\nUnder AEP's alert rules you qualify for a callback from a representative. "
                      "If anyone is in danger, call 911.")
        if not ok or not reply:
            result = self._offline_turn(s, message, newly_declared)
            result["note"] = f"LLM reply replaced: ungrounded numbers {bad}" if reply else "LLM returned no text"
            result["grounding_failed"] = True
            return result
        self._remember(s, message, reply)
        return {"reply": reply, "card": self._card(s), "tools_used": tools_used,
                "fallback": self._is_fallback(results), "mode": "llm", "provider": provider}

    # ---- offline / deterministic turn ----

    def _offline_turn(self, s: Session, message: str, newly_declared: list[str]) -> dict:
        intents = detect_intents(message)
        confirmed = self._confirmed_lines(self.storm.confirmed_for(s.premise))

        if "refuse_crews" in intents:
            reply = ("I can't share crew locations or assignments. That keeps crews safe and lets them work "
                     "without interruption. I can give you the estimated restoration range for your home instead.")
            return self._finish(s, message, reply, [], fallback=False)
        if "refuse_others" in intents:
            reply = ("I can only share information about your own service location. I can't share details "
                     "about other customers, medical registries, or critical facilities such as hospitals, "
                     "to protect privacy and safety.")
            return self._finish(s, message, reply, [], fallback=False)

        if not s.premise:
            reply = ("Confirmed by AEP: nothing yet; I need to know where you are first.\n"
                     "Model estimate: not available until you confirm your address.\n"
                     "What you can do: share your street address so I can look up your home. If you see a downed "
                     "line, stay 35 feet away and call 911.")
            return self._finish(s, message, reply, [], fallback=True)

        if self.storm.restored_for(s.premise) and not set(intents) - {"restoration", "risk", "what_if"}:
            reply = ("Confirmed by AEP: power has been restored to your home.\n"
                     "Model estimate: none needed now.\n"
                     "What you can do: if your power is still out, report it to AEP; it may be a separate problem "
                     "at your home. Reset clocks and check food that warmed above safe temperatures.")
            return self._finish(s, message, reply, [], fallback=False)
        if newly_declared and not intents:
            intents = ["prep", "alerts"]
        if not intents:
            intents = ["restoration"] if self.storm.outage_for(s.premise) else ["risk"]

        tools_used, estimate_lines, action_lines = [], [], []
        if "risk" in intents:
            r = self._run_tool(s, "get_outage_risk", {}, None)
            tools_used.append("get_outage_risk")
            if r.get("fallback"):
                return self._unknown_location(s, message, r, tools_used)
            estimate_lines.append(
                f"Outage risk at your home in this storm: {r['risk_band']} (about {round(r['probability'] * 100)}% "
                f"chance of an interruption). Confidence: {r['confidence']}. "
                + (" ".join(r["top_factors"][:2])))
        if "restoration" in intents or "what_if" in intents:
            overrides = parse_what_if(message, self.storm.scenario()) if "what_if" in intents else {}
            if "what_if" in intents and s.last_estimate is None:
                self._run_tool(s, "estimate_restoration", {}, None)
            e = self._run_tool(s, "estimate_restoration", overrides, None)
            tools_used.append("estimate_restoration")
            if e.get("fallback"):
                return self._unknown_location(s, message, e, tools_used)
            when = ("Your power is out; restoration" if self.storm.outage_for(s.premise)
                    else "If your power goes out, restoration")
            line = (f"{when} would most likely take about {e['p50_hrs']:g} hours (likely range "
                    f"{e['p10_hrs']:g}-{e['p90_hrs']:g} hours). The biggest factor is repairs at "
                    f"{e['bottleneck_device']}.")
            ch = e.get("change_vs_previous")
            if ch:
                direction = "later" if ch["p50_change_hrs"] > 0 else "sooner"
                line += (f" With that change, the most likely time moves {abs(ch['p50_change_hrs']):g} hours "
                         f"{direction} than the previous estimate of {ch['previous_p50_hrs']:g} hours.")
            estimate_lines.append(line)
        if "prep" in intents or newly_declared:
            g = self._run_tool(s, "prep_guidance", {}, None)
            tools_used.append("prep_guidance")
            action_lines += [f"[{a['category']}] {a['text']}" for a in g["actions"][:4]]
            if newly_declared:
                action_lines.insert(0, "Thanks for telling me; I've tailored these steps to what you shared.")
        if "alerts" in intents or newly_declared:
            a = self._run_tool(s, "check_alert_eligibility", {}, None)
            tools_used.append("check_alert_eligibility")
            if a["rules_fired"]:
                for rule in a["rules_fired"]:
                    action_lines.append(f"Alert rule: {rule['action']} by {rule['channel']} ({rule['timing']}).")
                if a["escalate_to_human"]:
                    action_lines.append("Under AEP's alert rules this qualifies for a callback from a representative. "
                                        "I can't schedule it myself; if anyone is in danger, call 911.")
            else:
                action_lines.append("No automatic alert applies right now, but you can sign up for outage texts.")
        if not action_lines:
            action_lines = ["[EMERGENCY] Stay at least 35 feet away from downed power lines and call 911.",
                            "Charge your phone and keep a flashlight handy."]

        reply = ("Confirmed by AEP: " + " ".join(confirmed) + "\n"
                 "Model estimate: " + (" ".join(estimate_lines) or "No new estimate for this question.") + "\n"
                 "What you can do: " + " ".join(action_lines))
        return self._finish(s, message, reply, tools_used, fallback=False)

    def _unknown_location(self, s, message, result, tools_used) -> dict:
        reply = ("Confirmed by AEP: nothing for this location.\n"
                 f"Model estimate: {result.get('message') or result.get('confidence_reason')}\n"
                 "What you can do: stay at least 35 feet away from downed lines and call 911 if you see one, "
                 "keep your phone charged, and report any outage to AEP.")
        return self._finish(s, message, reply, tools_used, fallback=True)

    def _finish(self, s: Session, message: str, reply: str, tools_used: list, fallback: bool) -> dict:
        self._remember(s, message, reply)
        return {"reply": reply, "card": self._card(s), "tools_used": tools_used, "fallback": fallback,
                "mode": "offline", "provider": None}

    # ---- shared ----

    def _remember(self, s: Session, message: str, reply: str) -> None:
        s.history += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
        s.history = s.history[-2 * HISTORY_TURNS:]

    @staticmethod
    def _confirmed_lines(c: dict) -> list[str]:
        if c.get("restored"):
            return ["Power has been restored to your home."]
        lines = []
        if c.get("storm_forecast"):
            lines.append("A storm is forecast or active in your area.")
        lines.append("An outage affecting your home is confirmed." if c.get("outage")
                     else "No outage has been confirmed at your home.")
        if c.get("damage"):
            parts = ", ".join(f"{v} {k.lower()}" for k, v in c["damage"].items())
            lines.append(f"Field crews assessed damage on your line ({parts}).")
        if c.get("crews_on_site"):
            lines.append("Crews are working on your line.")
        return lines

    def _card(self, s: Session) -> dict:
        """Built from tool results only: never from LLM text."""
        st = self.storm.state()
        risk = s.last_results.get("get_outage_risk")
        est = s.last_results.get("estimate_restoration")
        guide = s.last_results.get("prep_guidance")
        alerts = s.last_results.get("check_alert_eligibility")
        estimate = {}
        if risk and not risk.get("fallback"):
            estimate["risk"] = {k: risk.get(k) for k in ("risk_band", "probability", "confidence", "top_factors",
                                                          "severity")}
        if est and not est.get("fallback"):
            estimate["restoration"] = {k: est.get(k) for k in ("p10_hrs", "p50_hrs", "p90_hrs", "bottleneck_device",
                                                                "drivers", "change_vs_previous")}
        if estimate:
            estimate["label"] = "MODEL_ESTIMATE"
        actions = guide["actions"] if guide else []
        if alerts and alerts.get("rules_fired"):
            actions = actions + [{"category": "ALERT", "text": f"{r['action']} ({r['channel']}, {r['timing']})",
                                  "why": r["when"]} for r in alerts["rules_fired"]]
        return {"confirmed": self._confirmed_lines(self.storm.confirmed_for(s.premise)), "storm_step": st["step"],
                "estimate": estimate,
                "actions": actions, "declared_needs": list(s.needs)}

    @staticmethod
    def _is_fallback(results: list) -> bool:
        return any(isinstance(r, dict) and r.get("fallback") for r in results)
