"""How much data supports an answer: one 0-1 score, the missing data points, and what could change it.

This is a transparent data-support heuristic (documented in ml/ASSUMPTIONS.md), not a statistical
probability. Each missing piece of information subtracts a fixed amount; the biggest gaps are
listed so the customer knows what would make the answer more certain.
"""
BASE = {"risk": 0.95, "restoration": 0.90}
MAX_MISSING = 3
LOW_CONFIDENCE = 0.6


def _label(score: float) -> str:
    return "High" if score >= 0.75 else "Medium" if score >= LOW_CONFIDENCE else "Low"


def assess(kind: str, *, circuit_records: int, in_topology: bool, storm: dict,
           damage_assessed: bool = False, regional: bool = False) -> dict:
    """kind: 'risk' or 'restoration'. regional: a location outside AEP's data (a regional typical value)."""
    gaps = []  # (penalty, missing data point)
    if regional:
        gaps.append((0.35, "the local grid map for this location (it is outside AEP's data)"))
    if circuit_records < 5:
        gaps.append((0.30, "local storm history for this area" if regional
                     else "past storm damage records for the line that serves you"))
    elif circuit_records < 20:
        gaps.append((0.15, "more past storm records for the line that serves you"))
    if regional:
        gaps.append((0.15, "which utility serves this location"))
    elif not in_topology:
        gaps.append((0.15, "your home's exact connection in the grid map"))
    if not storm.get("forecast_available", True):
        gaps.append((0.25, "a live weather forecast"))
    elif not storm.get("hypothetical"):
        lead = storm.get("lead_days") or 0
        if lead >= 4:
            gaps.append((0.18, "a closer-range weather forecast (this day is several days out)"))
        elif lead >= 2:
            gaps.append((0.08, "a closer-range weather forecast"))
    if kind == "restoration":
        if not damage_assessed:
            gaps.append((0.15, "a field assessment of the actual damage on your line"))
        gaps.append((0.10, "how many crews will be assigned to your area"))
    gaps.append((0.05, "tree and vegetation data near your power lines"))

    score = max(0.05, min(0.99, BASE.get(kind, 0.9) - sum(p for p, _ in gaps)))
    missing = [m for _, m in sorted(gaps, key=lambda g: -g[0])][:MAX_MISSING]
    return {"score": round(score, 2), "label": _label(score), "missing": missing,
            "show_missing": score < LOW_CONFIDENCE}


def risk_changers(risk: dict, storm: dict) -> list[str]:
    """2-3 things that would change the outage-chance answer."""
    items = []
    if storm.get("hypothetical"):
        items.append("This is a hypothetical storm; a real storm's strength and track would change the odds")
    elif storm.get("drivers"):
        items.append(f"The forecast: {storm['drivers'][0]}. Stronger or weaker gusts than forecast move the odds most")
    factors = [f for f in risk.get("top_factors", []) if "storm damage report" not in f]
    if factors:
        items.append(factors[0])
    history = next((f for f in risk.get("top_factors", []) if "storm damage report" in f), None)
    if history:
        items.append(history)
    return items[:3]


def restoration_changers(est: dict) -> list[str]:
    """2-3 things that would change the repair-time answer."""
    items = []
    if est.get("bottleneck_device"):
        items.append(f"How badly {est['bottleneck_device']} is damaged (crews fix main-line devices first)")
    sc = est.get("scenario") or {}
    crews = sc.get("crews")
    delay = sc.get("crew_arrival_delay_hrs") or 0
    items.append(f"How many crews reach your area ({crews} assumed)" + (f" and when (after {delay:g} h)" if delay else "")
                 if crews else "How many crews reach your area and when")
    items.append("Fallen trees or blocked roads near your line, which add time to every repair")
    return items[:3]
