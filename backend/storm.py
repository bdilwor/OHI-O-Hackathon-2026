"""Demo storm timeline: pre_storm -> outage -> damage_assessed -> crews_working -> restored.

Each step sets what is *confirmed* (outage, assessed damage, crews on site) and the default
restoration scenario. Proactive messages are pushed to subscribers (SSE) on every advance.
This is a scripted demo state, clearly separate from model estimates.
"""
import asyncio
import copy
import json
from pathlib import Path

SCENARIO_PATH = Path(__file__).resolve().parent / "demo" / "scenario.json"
STEPS = ["pre_storm", "outage", "damage_assessed", "crews_working", "restored"]


class StormTimeline:
    def __init__(self, path: Path = SCENARIO_PATH):
        self.config = json.loads(Path(path).read_text())
        self.timeline = {s["step"]: s for s in self.config["timeline"]}
        assert [s["step"] for s in self.config["timeline"]] == STEPS, "scenario.json must list all steps in order"
        self.index = 0
        self.messages: list[dict] = []
        self.last_p50: dict[str, float] = {}  # premise -> last published P50, for ETR-slip alerts
        self._subscribers: set[asyncio.Queue] = set()

    @property
    def step(self) -> str:
        return STEPS[self.index]

    @property
    def current(self) -> dict:
        return self.timeline[self.step]

    def scenario(self) -> dict:
        return copy.deepcopy(self.current["scenario"])

    # Confirmed facts (outage, assessed damage, crews, restored) belong to the scripted demo home only.
    # Every other premise just sees that a storm is forecast or active.

    def applies_to(self, premise: str | None) -> bool:
        return premise is not None and str(premise) == self.config["premise"]

    def confirmed_for(self, premise: str | None) -> dict:
        c = copy.deepcopy(self.current["confirmed"])
        return c if self.applies_to(premise) else {"storm_forecast": c.get("storm_forecast", False), "outage": False}

    def outage_for(self, premise: str | None) -> bool:
        return bool(self.confirmed_for(premise).get("outage"))

    def restored_for(self, premise: str | None) -> bool:
        return bool(self.confirmed_for(premise).get("restored"))

    def hours_out_for(self, premise: str | None) -> float | None:
        return self.current["hours_since_outage"] if self.outage_for(premise) else None

    def confirmed_damage(self, premise: str | None) -> dict | None:
        return self.confirmed_for(premise).get("damage")

    def state(self) -> dict:
        return {
            "storm_name": self.config["storm_name"],
            "step": self.step,
            "step_index": self.index,
            "steps": STEPS,
            "confirmed": copy.deepcopy(self.current["confirmed"]),
            "hours_since_outage": self.current["hours_since_outage"],
            "scenario": self.scenario(),
            "label": "DEMO_TIMELINE",
        }

    def advance(self) -> dict:
        if self.index < len(STEPS) - 1:
            self.index += 1
        return self.state()

    def reset(self) -> dict:
        self.index = 0
        self.messages.clear()
        self.last_p50.clear()
        return self.state()

    # ---- proactive messages (SSE) ----

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, message: dict) -> None:
        self.messages.append(message)
        for q in list(self._subscribers):
            q.put_nowait(message)
