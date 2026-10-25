"""LLM middleware: tool-calling loop, grounding check, offline/demo-safe modes.

Every response is: a short direct answer (LLM or rules-based), plus deterministic metadata built
from tool results, never from LLM text:
  - question_type        risk | restoration | prep | alerts | other
  - what_could_change    2-3 things that would move an outage-chance / repair-time answer
  - confidence           one data-support score, with the missing data points when it is low

Providers (env LLM_PROVIDER): groq (default, falls back to gemini), gemini, offline.
Both cloud providers expose OpenAI-compatible endpoints, so one SDK covers them. Keys come only
from env (GROQ_API_KEY, GEMINI_API_KEY). With no key, or in tests, the offline router answers:
a deterministic keyword -> tools -> template pipeline that uses the same tools and rules.

The LLM never sees the premise number or any private field: tools are called with the session's
premise and storm setting injected server-side, and results pass through privacy.redact() first.
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
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-120b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "GEMINI_MODEL",
               "gemini-2.0-flash"),
}

SYSTEM_PROMPT = """You are AEP's AI Outage Concierge, helping one residential customer.

Rules you must always follow:
- Answer the customer's question directly in 1-3 short, plain sentences. No headings, no bullet lists.
- Never state a number, time, percentage or probability that did not come from a tool result in this conversation. If you need a number, call a tool.
- Do not list contributing factors, confidence levels or missing data: the app shows those separately.
- Outage chance and restoration times are model estimates: say "about", "most likely" and give the likely range for repair times. Don't mention percentiles.
- Mention which day or storm the answer is for, using the tool's storm "summary"/"when" wording; never call calm or breezy weather a storm.
- Write numbers as digits (1.5 hours, not 1½).
- Politely refuse requests for crew locations or assignments, other customers' information, or critical-facility details, and briefly say why (safety and privacy). Never confirm or deny whether anyone is on a medical or life-support registry.
- If a tool cannot estimate (unknown location, missing data), say so plainly and give the downed-line safety advice.
- Never claim to have dispatched a crew, scheduled a call, or made any commitment on AEP's behalf. Say what the customer "qualifies for", never that something "will" happen.
- Only say the power is on or off if the confirmed facts say so. Repair-time questions are always answerable: the estimate describes how long it would take if the power goes out.
- For "what if" questions, call estimate_restoration with the changed inputs and say how many hours later or sooner the most likely time is (from change_vs_previous).
- Special needs come only from what the customer tells you; never guess them.
- Don't give phone numbers or web addresses; say "report it to AEP". For danger, say "call 911".
- Add one safety sentence only when it is relevant to the question.
"""

_DAY = {"type": ["integer", "null"], "minimum": 0, "maximum": 6,
        "description": "Forecast day: 0 = today ... 6. Omit to use the customer's current setting."}
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_outage_risk",
        "description": "Chance the customer's home loses power, from the outage model and the live weather forecast "
                       "(riskiest day this week unless a day is given): risk band, probability, storm details.",
        "parameters": {"type": "object", "properties": {"day": _DAY}}}},
    {"type": "function", "function": {
        "name": "estimate_restoration",
        "description": "Restoration time range (P10/P50/P90 hours) if/while the customer's power is out. "
                       "Pass fields only to explore what-if scenarios.",
        "parameters": {"type": "object", "properties": {
            # Nullable: some models send null for every optional field, which strict validators reject otherwise.
            "day": _DAY,
            "crews": {"type": ["integer", "null"], "minimum": 1, "maximum": 50},
            "crew_arrival_delay_hrs": {"type": ["number", "null"], "minimum": 0, "maximum": 240},
            "extra_damage": {"type": ["object", "null"], "additionalProperties": {"type": "integer"},
                             "description": "Extra damage on the customer's line, e.g. {\"Pole\": 3, \"wire\": 1}"},
            "access_blocked": {"type": ["boolean", "null"]}}}}},
    {"type": "function", "function": {
        "name": "prep_guidance",
        "description": "Safety and preparation actions tailored to the risk, expected duration and the customer's "
                       "declared needs.",
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
    "what_if": [r"what if", r"suppose", r"delayed?", r"(crew|arriv|until|wait).*tomorrow",
                r"can'?t (get|arrive|reach)", r"more crews", r"fewer crews", r"road.*(blocked|closed)", r"blocked"],
    "restoration": [r"how long", r"restor", r"when will", r"back on", r"\betr\b", r"last\b", r"get power back",
                    r"repair"],
    "risk": [r"lose power", r"outage risk", r"chance", r"likel", r"will (i|my|we)", r"risk", r"go out"],
    "prep": [r"prepar", r"what should i do", r"get ready", r"\bsafe", r"tips", r"\bplan\b"],
    "alerts": [r"alert", r"notif", r"text me", r"qualif", r"sign up", r"call me"],
}


@dataclass
class Session:
    premise: str | None = None
    needs: list = field(default_factory=list)
    storm: dict | None = None  # the customer's map setting: forecast day or replay
    outage: dict | None = None  # Outage test tab: the simulated fault (confirmed outage for this session)
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


def day_from_message(message: str, forecast_days: list[dict]) -> int | None:
    """'today' / 'tomorrow' / a weekday name -> forecast day index."""
    m = message.lower()
    if re.search(r"\btoday\b|\btonight\b", m):
        return 0
    if re.search(r"\btomorrow\b", m):
        return 1
    named = [w[:3] for w in WEEKDAYS if re.search(rf"\b{w}\b", m)]
    return next((d["day"] for d in forecast_days if d["day"] and d["weekday"].lower() in named), None)


def parse_what_if(message: str, base: dict) -> dict:
    """Scenario overrides from a what-if question, relative to the current scenario."""
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
    return out


SAFE_NUMBERS = {911, 35}  # emergency number and downed-line distance from the safety rules
LLM_DROP_KEYS = {"rules_table", "assumptions", "source", "model_version", "scenario", "chars", "channel",
                 "opt_in_available", "label", "confidence_meter", "what_could_change", "top_factors", "drivers",
                 "risk_breakdown", "confidence_reason", "disclaimer"}
LLM_MAX_ITEMS = 6


def compact_for_llm(result):
    """Smaller tool payloads for the LLM (free-tier token limits). The app's metadata uses the full result."""
    if isinstance(result, dict):
        return {k: compact_for_llm(v) for k, v in result.items() if k not in LLM_DROP_KEYS and v is not None}
    if isinstance(result, list):
        return [compact_for_llm(v) for v in result[:LLM_MAX_ITEMS]]
    return result


def clean_tool_args(args: dict) -> dict:
    """Drop nulls/no-op values models fill in for optional fields, so they aren't treated as what-ifs."""
    return {k: v for k, v in (args or {}).items()
            if v is not None and v != {} and not (k == "access_blocked" and v is False)}


FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}


def _expand_fractions(text: str) -> str:
    """'1½' -> '1.5', '½' -> '0.5' so the grounding check sees the real value."""
    return re.sub(r"(\d*)\s?([½¼¾⅓⅔])", lambda m: f"{int(m.group(1) or 0) + FRACTIONS[m.group(2)]:g}", text)


def numbers_in(obj) -> set[float]:
    found = set()
    text = _expand_fractions(obj) if isinstance(obj, str) else json.dumps(obj)
    for n in re.findall(r"(?<![A-Za-z\d.])-?\d+(?:\.\d+)?", text):
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


def pct(p: float) -> str:
    v = p * 100
    return "under 1%" if v < 1 else f"about {v:.0f}%"


UNKNOWN_CONFIDENCE = {"score": 0.1, "label": "Low", "show_missing": True,
                      "missing": ["your home in our service records", "your home's connection in the grid map",
                                  "past storm records for your line"]}


class Assistant:
    def __init__(self, toolbox, storm):
        self.tools = toolbox
        self.storm = storm  # demo storm timeline (confirmed facts for the demo home)
        self.sessions: dict[str, Session] = {}
        self.provider = os.environ.get("LLM_PROVIDER", "groq").lower()
        self.cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}

    # ---- public ----

    def chat(self, message: str, session_id: str = "default", premise: str | None = None,
             declared_needs: list[str] | None = None, demo_safe: bool = False, storm: dict | None = None,
             outage: dict | None = None) -> dict:
        s = self.sessions.setdefault(session_id, Session())
        if premise:
            s.premise = str(premise)
        if declared_needs is not None:
            s.needs = [n for n in declared_needs if n in DECLARED_NEEDS]
        if storm is not None:
            s.storm = storm
        s.outage = outage
        newly_declared = [n for n in detect_needs(message) if n not in s.needs]
        s.needs += newly_declared

        if demo_safe:
            cached = self.cache.get(self.cache_key(message, s.storm))
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

    def cache_key(self, message: str, storm: dict | None = None) -> str:
        mode = (storm or {}).get("mode")
        tag = ("replay" if mode == "replay"
               else "sandbox" + "-".join(str((storm or {}).get(k)) for k in ("intensity", "crews", "duration_days"))
               if mode == "sandbox" else f"day{(storm or {}).get('day', 'auto')}")
        return f"{self.storm.step}|{tag}|{re.sub(r'[^a-z0-9 ]', '', message.lower()).strip()}"

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

    # ---- tool execution (premise, needs and storm setting injected server-side) ----

    def _storm_for(self, s: Session, day: int | None) -> dict | None:
        if day is not None and (s.storm or {}).get("mode", "forecast") == "forecast":
            return {"mode": "forecast", "day": int(day)}
        return s.storm

    def _run_tool(self, s: Session, name: str, args: dict, provider: str | None) -> dict:
        args = clean_tool_args(args)
        storm = self._storm_for(s, args.pop("day", None))
        if name == "get_outage_risk":
            call_args = {"premise": s.premise, "storm": storm}
        elif name == "estimate_restoration":
            call_args = {"premise": s.premise, "scenario": args, "storm": storm}
        elif name in ("prep_guidance", "check_alert_eligibility"):
            call_args = {"premise": s.premise, "declared_needs": s.needs, "storm": storm}
        else:
            call_args = {"premise": s.premise, "storm": storm, **args}
        source = "sandbox" if (storm or {}).get("mode") == "sandbox" else "outage_test" if s.outage else "chat"
        if name == "estimate_restoration" and s.outage:  # confirmed outage: its known damage drives the estimate
            name, call_args = "outage_restoration", {"premise": s.premise, "outage": s.outage}
        result = self.tools.call(name, call_args, source=source, provider=provider)
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

    def _outage_record(self, s: Session) -> dict | None:
        if not (s.outage and s.premise and self.tools.outages):
            return None
        try:
            return self.tools.outages.test(s.premise, **s.outage)
        except (LookupError, ValueError):
            return None

    def _context_notes(self, s: Session) -> list[dict]:
        facts = " ".join(self._confirmed_lines(self.storm.confirmed_for(s.premise)))
        rec = self._outage_record(s)
        if rec and rec["affects_you"]:
            facts = (f"An outage affecting the customer's home is confirmed (cause: {rec['cause']}; location: "
                     f"{rec['location']}; reported {rec['reported_display']}). Their power is out now. "
                     f"Call estimate_restoration for the expected fix time.")
        needs = "; ".join("they " + DECLARED_NEEDS[n] for n in s.needs) or "none"
        return [
            {"role": "system", "content": f"Confirmed by AEP right now (nothing else is confirmed): {facts}"},
            {"role": "system", "content": f"Background: the customer's home is identified. What the customer told "
                                          f"you about their needs: {needs}."},
        ]

    def _llm_turn(self, s: Session, message: str, providers: list[str], newly_declared: list[str]) -> dict:
        if not s.premise:
            return self._offline_turn(s, message, newly_declared)
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    *self._context_notes(s),
                    *s.history[-2 * HISTORY_TURNS:],
                    {"role": "user", "content": message}]
        tools_used, results, provider = [], [], None
        # The core questions never depend on the LLM choosing to call the model: run the matching tools first.
        intents = detect_intents(message)
        pre = []
        if not {"refuse_crews", "refuse_others"} & set(intents):
            days = self.tools.forecast.forecast(*self.tools.latlon(s.premise)).get("days", [])
            day = day_from_message(message, days)
            if "risk" in intents:
                pre.append(("get_outage_risk", {"day": day}))
            if {"restoration", "what_if"} & set(intents):
                if "what_if" in intents and s.last_estimate is None:
                    self._run_tool(s, "estimate_restoration", {"day": day}, None)  # baseline for the comparison
                overrides = parse_what_if(message, self.storm.scenario()) if "what_if" in intents else {}
                pre.append(("estimate_restoration", {**overrides, "day": day}))
        if newly_declared:  # a newly declared need always gets tailored guidance + the alert/escalation rules
            pre += [("prep_guidance", {}), ("check_alert_eligibility", {})]
        if pre:
            fetched = {}
            for name, args in pre:
                out = self._run_tool(s, name, args, None)
                tools_used.append(name)
                results.append(out)
                fetched[name] = compact_for_llm(out)
            messages.insert(-1, {"role": "system", "content": (
                "Tool results already retrieved for this question (answer from them; call other tools only if "
                "needed; mention any escalation the alert rules recommend, without promising it): "
                + json.dumps(fetched))})
        reply = None
        # When the backend already fetched what the question needs, answer in one call without tool
        # definitions: about half the tokens (free tiers allow ~8k tokens/minute).
        offer_tools = not pre
        while True:
            budget_left = MAX_TOOL_CALLS - len(tools_used)
            msg, provider = self.call_llm(messages, TOOL_SPECS if budget_left > 0 and offer_tools else None, providers)
            offer_tools = True
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
            reply += (" Under AEP's alert rules you qualify for a callback from a representative. "
                      "If anyone is in danger, call 911.")
        if not ok or not reply:
            result = self._offline_turn(s, message, newly_declared)
            result["note"] = f"LLM reply replaced: ungrounded numbers {bad}" if reply else "LLM returned no text"
            result["grounding_failed"] = True
            return result
        self._remember(s, message, reply)
        return {"reply": reply, **self._meta(s, intents, tools_used, results),
                "tools_used": tools_used, "fallback": self._is_fallback(results), "mode": "llm", "provider": provider}

    # ---- offline / deterministic turn ----

    def _offline_turn(self, s: Session, message: str, newly_declared: list[str]) -> dict:
        intents = detect_intents(message)

        if "refuse_crews" in intents:
            return self._finish(s, message, "I can't share crew locations or assignments; that keeps crews safe and "
                                "lets them work without interruption. I can tell you how long repairs near you "
                                "are likely to take instead.", intents, [], [], fallback=False)
        if "refuse_others" in intents:
            return self._finish(s, message, "I can only share information about your own home. I can't share "
                                "details about other customers, medical registries, or critical facilities such as "
                                "hospitals, to protect privacy and safety.", intents, [], [], fallback=False)
        if not s.premise:
            return self._finish(s, message, "Please find your home first so I can check your outage risk. If you see "
                                "a downed line, stay 35 feet away and call 911.", intents, [], [], fallback=True)
        rec = self._outage_record(s)
        if rec and rec["affects_you"] and "risk" in intents and not {"restoration", "what_if"} & set(intents):
            e = self._run_tool(s, "estimate_restoration", {}, None)
            return self._finish(s, message, f"Yes: your power is out right now ({rec['cause'].lower()}, reported "
                                f"{rec['reported_display']}). It should most likely be back by {e['most_likely_fixed_by']}.",
                                ["restoration"], ["outage_restoration"], [e], fallback=False)
        if self.storm.restored_for(s.premise) and not set(intents) - {"restoration", "risk", "what_if"}:
            return self._finish(s, message, "Yes: power has been restored to your home. If yours is still out, "
                                "report it to AEP; it may be a separate problem at your home.",
                                intents, [], [], fallback=False)
        if newly_declared and not intents:
            intents = ["prep", "alerts"]
        if not intents:
            intents = ["restoration"] if self.storm.outage_for(s.premise) else ["risk"]

        days = self.tools.forecast.forecast(*self.tools.latlon(s.premise)).get("days", [])
        day = day_from_message(message, days)
        tools_used, results, sentences = [], [], []

        if "risk" in intents:
            r = self._run_tool(s, "get_outage_risk", {"day": day}, None)
            tools_used.append("get_outage_risk")
            results.append(r)
            if r.get("fallback"):
                return self._unknown_location(s, message, intents, tools_used, results)
            st = r["storm"]
            worst = st.get("mode") == "forecast" and day is None and (s.storm or {}).get("day") is None
            sentences.append(
                f"Your chance of losing power {st.get('when') or 'in this storm'} is {r['risk_band'].lower()}: "
                f"{pct(r['probability'])}" + (", the riskiest day in this week's forecast." if worst else ".")
                + (" This is a hypothetical storm, not the forecast." if st.get("hypothetical") else ""))
        if "restoration" in intents or "what_if" in intents:
            overrides = parse_what_if(message, self.storm.scenario()) if "what_if" in intents else {}
            if "what_if" in intents and s.last_estimate is None:
                self._run_tool(s, "estimate_restoration", {"day": day}, None)
            e = self._run_tool(s, "estimate_restoration", {**overrides, "day": day}, None)
            tools_used.append("estimate_restoration")
            results.append(e)
            if e.get("fallback"):
                return self._unknown_location(s, message, intents, tools_used, results)
            if e.get("outage_confirmed"):
                text = (f"Your outage ({e['cause'].lower()}) was reported {e['reported']}. Power should most likely "
                        f"be back by {e['most_likely_fixed_by']} (likely between {e['fixed_between'][0]} and "
                        f"{e['fixed_between'][1]}), about {e['p50_hrs']:g} hours after it was reported.")
            else:
                lead = ("Your outage is confirmed; power" if self.storm.outage_for(s.premise)
                        else "If your power goes out, it")
                text = (f"{lead} would most likely be back in about {e['p50_hrs']:g} hours "
                        f"(likely {e['p10_hrs']:g}-{e['p90_hrs']:g} hours).")
            ch = e.get("change_vs_previous")
            if ch and ch["p50_change_hrs"]:
                text += (f" That's {abs(ch['p50_change_hrs']):g} hours "
                         f"{'later' if ch['p50_change_hrs'] > 0 else 'sooner'} than before.")
            sentences.append(text)
        if "prep" in intents or newly_declared:
            g = self._run_tool(s, "prep_guidance", {}, None)
            tools_used.append("prep_guidance")
            results.append(g)
            need_items = [a["text"] for a in g["actions"] if a["why"].startswith("you told us")]
            top = need_items[:1] + [a["text"] for a in g["actions"] if a["text"] not in need_items][:2]
            sentences.append(("Thanks for telling me. " if newly_declared else "") + " ".join(top))
        if "alerts" in intents or newly_declared:
            a = self._run_tool(s, "check_alert_eligibility", {}, None)
            tools_used.append("check_alert_eligibility")
            results.append(a)
            if a["escalate_to_human"]:
                sentences.append("Under AEP's alert rules you qualify for a callback from a representative; "
                                 "I can't schedule it myself. If anyone is in danger, call 911.")
            elif a["rules_fired"]:
                rule = a["rules_fired"][0]
                sentences.append(f"Yes, you qualify: {rule['action'].lower()} by {rule['channel']} "
                                 f"({rule['timing'].lower()}).")
            else:
                sentences.append("No automatic alert applies right now, but you can sign up for outage texts.")
        return self._finish(s, message, " ".join(sentences), intents, tools_used, results, fallback=False)

    def _unknown_location(self, s, message, intents, tools_used, results) -> dict:
        reply = ("I couldn't find your home in our records, so I can't give a reliable estimate. If you see a "
                 "downed line, stay 35 feet away and call 911, and report any outage to AEP.")
        return self._finish(s, message, reply, intents, tools_used, results, fallback=True)

    def _finish(self, s: Session, message: str, reply: str, intents: list, tools_used: list, results: list,
                fallback: bool) -> dict:
        self._remember(s, message, reply)
        return {"reply": reply, **self._meta(s, intents, tools_used, results), "tools_used": tools_used,
                "fallback": fallback, "mode": "offline", "provider": None}

    # ---- deterministic metadata: question type, what could change it, confidence ----

    def _meta(self, s: Session, intents: list, tools_used: list, results: list) -> dict:
        by_tool = {}
        for name, r in zip(tools_used, results):
            by_tool.setdefault(name, r)
        est = by_tool.get("estimate_restoration") or by_tool.get("outage_restoration")
        risk = by_tool.get("get_outage_risk")
        if any(isinstance(r, dict) and r.get("fallback") for r in results):
            return {"question_type": "other", "what_could_change": [], "confidence": UNKNOWN_CONFIDENCE,
                    "storm": (risk or est or {}).get("storm"), "declared_needs": list(s.needs)}
        if est and ({"restoration", "what_if"} & set(intents) or not risk):
            qtype, primary = "restoration", est
        elif risk:
            qtype, primary = "risk", risk
        elif "prep_guidance" in by_tool or "check_alert_eligibility" in by_tool:
            qtype = "alerts" if "check_alert_eligibility" in by_tool and "prep" not in intents else "prep"
            primary = self.tools.get_outage_risk(s.premise, storm=s.storm) if s.premise else None
        else:
            qtype, primary = "other", None
        changers = primary.get("what_could_change", []) if primary and qtype in ("risk", "restoration") else []
        return {"question_type": qtype, "what_could_change": changers,
                "confidence": (primary or {}).get("confidence_meter"),
                "storm": (primary or {}).get("storm"), "declared_needs": list(s.needs),
                "bottleneck": (est or {}).get("bottleneck_device") if qtype == "restoration" else None}

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

    @staticmethod
    def _is_fallback(results: list) -> bool:
        return any(isinstance(r, dict) and r.get("fallback") for r in results)
