"""AI Outage Concierge API.

    uvicorn main:app --host 0.0.0.0 --port 8000

Heavy state (DB engine, risk model + topology graph, labor model, storm timeline) loads once at startup.
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lookup import Lookup
from mapdata import MapData
from ops import OpsService
from outages import CAUSES, OutageService
from ml.db import get_engine
from ml.score import PremiseScorer
from forecast import ForecastService
from privacy import default_audit_log
from regional import RegionalService, is_regional
from restoration import RestorationEstimator
from storm import StormTimeline
from tools import ToolBox

STATE: dict = {}


def build_state() -> dict:
    t0 = time.time()
    engine = get_engine()
    scorer = PremiseScorer(engine)
    estimator = RestorationEstimator(engine, scorer)
    storm = StormTimeline()
    audit = default_audit_log()
    toolbox = ToolBox(engine, scorer, estimator, storm, audit, ForecastService(scorer.levels))
    from assistant import Assistant  # imported late: optional LLM SDK

    state = {"engine": engine, "scorer": scorer, "estimator": estimator, "storm": storm, "audit": audit,
             "tools": toolbox, "lookup": Lookup(engine, scorer), "assistant": Assistant(toolbox, storm),
             "map": MapData(engine, scorer)}
    state["outages"] = OutageService(engine, scorer, estimator, state["map"], storm)
    toolbox.outages = state["outages"]
    state["regional"] = toolbox.regional = RegionalService(engine, scorer, estimator)
    state["ops"] = OpsService(engine, scorer, estimator)
    print(f"Startup complete in {time.time() - t0:.1f}s")
    return state


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE.update(build_state())
    yield
    STATE.clear()


app = FastAPI(title="AI Outage Concierge", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])


# ---- request models ----

class StormContext(BaseModel):
    """Live forecast day (default: riskiest day this week), a replay of a past storm, or a sandbox simulation."""
    mode: str = Field("forecast", pattern="^(forecast|replay|sandbox)$")
    day: int | None = Field(None, ge=0, le=6)
    storm: str | None = Field(None, pattern="^(feb2025|sep2024)$")
    # sandbox sliders (demo/testing; nothing is stored)
    intensity: int | None = Field(None, ge=1, le=5)
    crews: int | None = Field(None, ge=1, le=30)
    duration_days: int | None = Field(None, ge=1, le=5)
    radius_m: float | None = Field(None, ge=100, le=5000)


class RiskRequest(BaseModel):
    premise: str
    severity: str | None = Field(None, pattern="^(low|medium|high)$")
    storm: StormContext | None = None


class RestorationRequest(BaseModel):
    premise: str
    storm_severity: str | None = Field(None, pattern="^(low|med|medium|high)$")
    crews: int | None = Field(None, ge=1, le=50)
    crew_arrival_delay_hrs: float | None = Field(None, ge=0, le=240)
    extra_damage: dict[str, int] | None = None
    access_blocked: bool | None = None
    storm: StormContext | None = None


class NeedsRequest(BaseModel):
    premise: str
    declared_needs: list[str] = []
    storm: StormContext | None = None


class MessageRequest(BaseModel):
    premise: str
    kind: str = Field(..., pattern="^(pre_storm|outage_confirmed|estimate_updated|restored)$")
    storm: StormContext | None = None


class OutageTest(BaseModel):
    """A simulated fault for the Outage test tab: a supply-path device index or a clicked map point."""
    cause: str = Field("tree", pattern="^(" + "|".join(CAUSES) + ")$")
    device: int | None = Field(None, ge=0, le=20)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    reported_hours_ago: float = Field(0, ge=0, le=72)
    crews: int = Field(2, ge=1, le=30)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    session_id: str = "default"
    premise: str | None = None
    declared_needs: list[str] | None = None
    demo_safe: bool = False
    storm: StormContext | None = None
    outage: OutageTest | None = None
    audience: str = Field("customer", pattern="^(customer|operations)$")  # chat voice: customer or AEP staff


def run_tool(name: str, args: dict) -> dict:
    try:
        return STATE["tools"].call(name, args, source="api")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


# ---- lookup ----

@app.get("/lookup")
def lookup(address: str | None = None, lat: float | None = Query(None, ge=-90, le=90),
           lon: float | None = Query(None, ge=-180, le=180)):
    lk = STATE["lookup"]
    try:
        if address:
            result = lk.by_address(address)
        elif lat is not None and lon is not None:
            result = lk.by_point(lat, lon)
        else:
            raise HTTPException(422, "Provide ?address= or ?lat=&lon=")
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    STATE["audit"].record("lookup", {"address": bool(address), "point": lat is not None}, result,
                          [], source="api")
    return result


@app.get("/regional/locate")
def regional_locate(address: str | None = Query(None, min_length=3, max_length=200),
                    lat: float | None = Query(None, ge=-90, le=90), lon: float | None = Query(None, ge=-180, le=180)):
    """A regional-estimate location for a place outside AEP's data (geocoded city/state, or a map point)."""
    if not address and (lat is None or lon is None):
        raise HTTPException(422, "Provide ?address= or ?lat=&lon=")
    result = STATE["regional"].locate(address=address, lat=lat, lon=lon)
    STATE["audit"].record("regional_locate", {"address": bool(address), "point": lat is not None}, result, [],
                          source="api")
    return result


# ---- map ----

@app.get("/map/context")
def map_context(premise: str, mode: str = Query("forecast", pattern="^(forecast|replay|sandbox)$"),
                day: int = Query(0, ge=0, le=6), storm: str = Query("feb2025", pattern="^(feb2025|sep2024)$"),
                intensity: int = Query(3, ge=1, le=5), crews: int = Query(4, ge=1, le=30),
                duration_days: int = Query(1, ge=1, le=5), radius_m: float = Query(1500, ge=100, le=5000)):
    """The model's prediction around the customer's own home for one forecast day or a replayed storm.

    Homes, equipment and lines for the 2D/3D map; never sent to the LLM.
    """
    tools = STATE["tools"]
    ctx = ({"mode": "replay", "storm": storm} if mode == "replay"
           else {"mode": "sandbox", "intensity": intensity, "crews": crews, "duration_days": duration_days,
                 "radius_m": radius_m} if mode == "sandbox"
           else {"mode": "forecast", "day": day})
    try:
        st = tools.resolve_storm(premise, ctx)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    risk = tools.get_outage_risk(premise, storm=ctx)
    if is_regional(premise):  # outside AEP's data: basemap + home marker only, no equipment
        payload = STATE["regional"].map_payload(premise, risk.get("probability"))
    else:
        payload = STATE["map"].context(premise, st["storm_n"], risk.get("probability"), area=st.get("area"))
    if payload is None:
        raise HTTPException(404, "We couldn't find this service location in our records.")
    lat, lon = tools.latlon(premise)
    payload["storm"] = tools._storm_public(st)
    payload["forecast"] = tools.forecast.forecast(lat, lon)
    payload["your_risk"] = {k: risk.get(k) for k in ("risk_band", "probability", "confidence_meter", "scope",
                                                     "probability_range")}
    est = tools.estimate_restoration(premise, storm=ctx)
    payload["restoration"] = {k: est.get(k) for k in ("p10_hrs", "p50_hrs", "p90_hrs", "bottleneck_device")}
    payload["demo_home"] = STATE["storm"].applies_to(premise)  # real outage events (SSE) are for this home only
    STATE["audit"].record("map_context", {"premise": premise, **ctx},
                          {k: v for k, v in payload.items() if k != "home"}, [],
                          source="sandbox" if mode == "sandbox" else "api")
    return payload


# ---- AEP operations (internal) ----

@app.get("/ops/overview")
def ops_overview(mode: str = Query("forecast", pattern="^(forecast|sandbox)$"), day: int = Query(0, ge=0, le=6),
                 intensity: int = Query(3, ge=1, le=5), duration_days: int = Query(1, ge=1, le=5),
                 crews: int | None = Query(None, ge=1, le=200)):
    """Area-wide storm impact for AEP staff: KPIs, riskiest circuits, priority facilities. Read-only."""
    tools = STATE["tools"]
    ctx = ({"mode": "sandbox", "intensity": intensity, "duration_days": duration_days} if mode == "sandbox"
           else {"mode": "forecast", "day": day})
    st = tools.resolve_storm(None, ctx)
    if mode == "sandbox":  # area-wide, not centered on a home
        st["summary"] = (f"a simulated category {intensity} storm ({st['label'].lower()}) lasting {duration_days} "
                         f"day{'s' if duration_days > 1 else ''} across the service area")
    out = STATE["ops"].overview(tools._storm_public(st), crews)
    STATE["audit"].record("ops_overview", ctx, {"kpis": out["kpis"]}, [], source="ops")
    return out


# ---- outages ----

def _outage_call(fn, audit_tool: str, audit_input: dict, source: str):
    try:
        out = fn()
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    STATE["audit"].record(audit_tool, audit_input, out if isinstance(out, dict) else {}, [], source=source)
    return out


@app.get("/outages/current")
def outage_current(premise: str):
    """Current outage affecting the customer's home from the outage feed (None when there isn't one)."""
    return {"outage": _outage_call(lambda: STATE["outages"].current(premise), "outage_current",
                                   {"premise": premise}, "api")}


@app.get("/outage-test/devices")
def outage_test_devices(premise: str):
    return {"devices": _outage_call(lambda: STATE["outages"].path_devices(premise), "outage_test_devices",
                                    {"premise": premise}, "outage_test")}


@app.get("/outage-test")
def outage_test(premise: str, cause: str = Query("tree", pattern="^(" + "|".join(CAUSES) + ")$"),
                device: int | None = Query(None, ge=0, le=20), lat: float | None = Query(None, ge=-90, le=90),
                lon: float | None = Query(None, ge=-180, le=180),
                reported_hours_ago: float = Query(0, ge=0, le=72), crews: int = Query(2, ge=1, le=30)):
    """Simulated outage (Outage test tab). Stateless and read-only: recomputed on every call, never stored."""
    args = {"cause": cause, "device": device, "lat": lat, "lon": lon, "reported_hours_ago": reported_hours_ago,
            "crews": crews}
    return _outage_call(lambda: STATE["outages"].test(premise, **args), "outage_test", {"premise": premise, **args},
                        "outage_test")


# ---- tools ----

@app.post("/tools/get_outage_risk")
def get_outage_risk(req: RiskRequest):
    return run_tool("get_outage_risk", req.model_dump(exclude_none=True))


@app.get("/forecast")
def forecast(premise: str | None = None):
    """The live 7-day forecast and the storm size each day implies for the outage model."""
    lat, lon = STATE["tools"].latlon(premise)
    return STATE["tools"].forecast.forecast(lat, lon)


@app.post("/tools/estimate_restoration")
def estimate_restoration(req: RestorationRequest):
    scenario = req.model_dump(exclude_none=True, exclude={"premise", "storm"})
    storm = req.storm.model_dump() if req.storm else None
    return run_tool("estimate_restoration", {"premise": req.premise, "scenario": scenario, "storm": storm})


@app.post("/tools/prep_guidance")
def prep_guidance(req: NeedsRequest):
    return run_tool("prep_guidance", req.model_dump())


@app.post("/tools/check_alert_eligibility")
def check_alert_eligibility(req: NeedsRequest):
    return run_tool("check_alert_eligibility", req.model_dump())


@app.post("/tools/draft_message")
def draft_message(req: MessageRequest):
    return run_tool("draft_message", req.model_dump())


# ---- storm timeline (demo) ----

@app.get("/storm/state")
def storm_state():
    return STATE["storm"].state()


@app.post("/storm/advance")
def storm_advance():
    storm = STATE["storm"]
    before = storm.step
    state = storm.advance()
    if state["step"] != before:
        _push_proactive_message()
    return {**state, "messages": storm.messages[-1:]}


@app.post("/storm/reset")
def storm_reset():
    state = STATE["storm"].reset()
    _push_proactive_message()
    return state


def _push_proactive_message() -> None:
    """Draft the message for the new step (demo premise) and push it as an assistant-initiated chat message."""
    storm, tools = STATE["storm"], STATE["tools"]
    premise = storm.config["premise"]
    kind = storm.current["proactive_message"]
    msg = tools.call("draft_message", {"premise": premise, "kind": kind}, source="storm")
    if kind in ("outage_confirmed", "estimate_updated"):
        est = tools.estimate_restoration(premise)
        if est.get("p50_hrs") is not None:
            storm.last_p50[premise] = est["p50_hrs"]
    storm.publish({"role": "assistant", "proactive": True, "step": storm.step, **msg})


@app.get("/storm/events")
async def storm_events():
    """Server-sent events: one `data:` line per proactive message."""
    storm = STATE["storm"]
    q = storm.subscribe()

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'hello', 'step': storm.step})}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(msg)}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            storm.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/storm/messages")
def storm_messages():
    """Polling alternative to /storm/events."""
    return {"messages": STATE["storm"].messages}


# ---- chat + audit ----

@app.post("/chat")
def chat(req: ChatRequest):
    try:
        return STATE["assistant"].chat(req.message, session_id=req.session_id, premise=req.premise,
                                       declared_needs=req.declared_needs, demo_safe=req.demo_safe,
                                       storm=req.storm.model_dump() if req.storm else None,
                                       outage=req.outage.model_dump() if req.outage else None,
                                       audience=req.audience)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/audit")
def audit(limit: int = Query(100, ge=1, le=500)):
    return {"entries": STATE["audit"].recent(limit)}


@app.get("/health")
def health():
    return {"ok": bool(STATE), "model_version": STATE["scorer"].bundle["model_version"] if STATE else None}

