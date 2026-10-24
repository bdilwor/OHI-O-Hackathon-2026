"""Rules-based preparation guidance, alert eligibility and customer message templates.

Everything here is a documented rule, not model output. Special needs come ONLY from what the
customer declares; nothing is inferred.
"""
DECLARED_NEEDS = {
    "medical_device": "use powered medical equipment",
    "refrigerated_meds": "keep medicine that must stay refrigerated",
    "well_pump": "rely on an electric well pump for water",
    "sump_pump": "rely on a sump pump",
    "mobility": "have mobility needs",
}
MEDICAL_NEEDS = {"medical_device", "refrigerated_meds"}
HIGH_BANDS = {"High", "Very High"}
SMS_LIMIT = 320


def duration_bucket(p90_hrs: float | None) -> str:
    if p90_hrs is None:
        return "unknown"
    return "short" if p90_hrs < 4 else "day" if p90_hrs <= 24 else "multi_day"


def validate_needs(needs) -> list[str]:
    needs = list(dict.fromkeys(needs or []))
    unknown = [n for n in needs if n not in DECLARED_NEEDS]
    if unknown:
        raise ValueError(f"unknown declared needs {unknown}; use {sorted(DECLARED_NEEDS)}")
    return needs


# ---- preparation guidance ----------------------------------------------------------------------

def prep_guidance(risk_band: str | None, p90_hrs: float | None, declared_needs: list[str],
                  outage_active: bool = False) -> dict:
    needs = validate_needs(declared_needs)
    bucket = duration_bucket(p90_hrs)
    items = [
        ("EMERGENCY", "Stay at least 35 feet away from downed power lines and anything touching them. "
                      "Call 911, then report the line to AEP.", "always"),
        ("SAFETY", "Never run a generator indoors or in a garage; keep it outside, away from windows.", "always"),
    ]
    if risk_band in HIGH_BANDS or outage_active:
        items += [
            ("PREPARE", "Charge phones and battery packs now, and keep a flashlight with spare batteries.",
             f"risk is {risk_band}" if risk_band else "outage in progress"),
            ("PREPARE", "Keep 1 gallon of water per person per day and food that needs no cooking.",
             "outages may last a while"),
        ]
    elif risk_band in {"Low", "Medium"}:
        items.append(("PREPARE", "Charge your phone and know where your flashlight is.", f"risk is {risk_band}"))
    items.append(("SAFETY", "Keep the fridge and freezer closed: food stays cold about 4 hours in the fridge and "
                            "48 hours in a full freezer.", "food safety"))
    if bucket in ("day", "multi_day"):
        items.append(("PREPARE", "Plan where you could go to stay warm or cool, and fill your car's gas tank.",
                      "restoration could take a day or more"))
    if bucket == "multi_day":
        items.append(("PREPARE", "Consider staying with family or friends if you can, and check on neighbors.",
                      "restoration could take more than a day"))

    need_items = {
        "medical_device": ("EMERGENCY", "Make sure your medical equipment has charged backup batteries and plan "
                                        "where you would go if they run low. If you can't breathe or are in "
                                        "danger, call 911."),
        "refrigerated_meds": ("PREPARE", "Have a cooler and ice packs ready for your medicine, and ask your "
                                         "pharmacist how long it stays safe without refrigeration."),
        "well_pump": ("PREPARE", "Fill bathtubs and containers with water now; your well pump won't run "
                                 "without power."),
        "sump_pump": ("PREPARE", "Check for a battery-backup sump pump and move valuables off the basement floor."),
        "mobility": ("PREPARE", "Charge powered mobility equipment fully and plan a way out that doesn't need "
                                "an elevator or stair lift."),
    }
    for need in needs:
        cat, text = need_items[need]
        items.append((cat, text, "you told us you " + DECLARED_NEEDS[need]))

    order = {"EMERGENCY": 0, "SAFETY": 1, "PREPARE": 2}
    items.sort(key=lambda i: order[i[0]])
    return {
        "risk_band": risk_band,
        "duration_bucket": bucket,
        "declared_needs": needs,
        "actions": [{"category": c, "text": t, "why": w} for c, t, w in items],
        "source": "rules",
    }


# ---- alert eligibility ---------------------------------------------------------------------------

ALERT_RULES = [
    {"id": "R1_PRE_STORM_HIGH_RISK", "when": "Storm forecast and outage risk is High or Very High",
     "action": "Send pre-storm alert", "channel": "SMS + email", "timing": "24 hours before the storm",
     "test": lambda c: c["step"] == "pre_storm" and c["risk_band"] in HIGH_BANDS},
    {"id": "R2_PRE_STORM_DECLARED_MEDICAL", "when": "Storm forecast and customer declared a medical need",
     "action": "Send pre-storm alert and offer a check-in call", "channel": "SMS + phone call",
     "timing": "24-48 hours before the storm",
     "test": lambda c: c["step"] == "pre_storm" and bool(set(c["needs"]) & MEDICAL_NEEDS)},
    {"id": "R3_OUTAGE_CONFIRMED", "when": "Outage confirmed at the customer's service location",
     "action": "Send outage-confirmed message with estimate range", "channel": "SMS + app push",
     "timing": "Immediately", "test": lambda c: c["outage"]},
    {"id": "R4_ESTIMATE_SLIPPED", "when": "Estimated restoration moved later by more than 2 hours",
     "action": "Send estimate-updated message", "channel": "SMS", "timing": "Immediately",
     "test": lambda c: c["outage"] and (c["etr_slip_hrs"] or 0) > 2},
    {"id": "R5_MEDICAL_LONG_OUTAGE", "when": "Declared medical need and outage expected or lasting over 12 hours",
     "action": "Escalate to a human callback", "channel": "Phone call from a representative",
     "timing": "Within 1 hour",
     "test": lambda c: bool(set(c["needs"]) & MEDICAL_NEEDS) and c["outage"]
                       and max(c["hours_out"] or 0, c["p50_hrs"] or 0) > 12},
    {"id": "R6_RESTORED", "when": "Power restored", "action": "Send restoration-complete message",
     "channel": "SMS", "timing": "Immediately", "test": lambda c: c.get("restored", False)},
]


def check_alert_eligibility(ctx: dict, internal_priority: bool = False) -> dict:
    """ctx: step, risk_band, needs, outage, restored, etr_slip_hrs, hours_out, p50_hrs."""
    fired = [{k: r[k] for k in ("id", "when", "action", "channel", "timing")} for r in ALERT_RULES if r["test"](ctx)]
    escalate = any(r["id"] == "R5_MEDICAL_LONG_OUTAGE" for r in fired)
    return {
        "eligible": bool(fired),
        "rules_fired": fired,
        "escalate_to_human": escalate,
        "opt_in_available": True,
        "note": "Any customer can sign up for outage text alerts." if not fired else None,
        "rules_table": [{k: r[k] for k in ("id", "when", "action", "channel", "timing")} for r in ALERT_RULES],
        # Backend-only: internal registry flag can raise dispatch priority. Stripped by privacy.redact().
        "_internal": {"registry_priority": bool(internal_priority)},
        "source": "rules",
    }


# ---- message templates ---------------------------------------------------------------------------

def _range(est: dict | None) -> str:
    if not est or est.get("p10_hrs") is None:
        return "We can't estimate restoration time yet."
    return f"Estimate (model, not a promise): {est['p10_hrs']:g}-{est['p90_hrs']:g} hrs, most likely ~{est['p50_hrs']:g} hrs."


def draft_message(kind: str, risk: dict | None = None, est: dict | None = None,
                  previous_p50: float | None = None) -> dict:
    if kind == "pre_storm":
        band = (risk or {}).get("risk_band", "Unknown")
        text = (f"AEP storm alert: a storm is expected in your area. Outage risk at your home: {band} (estimate). "
                "Charge devices, keep a flashlight handy, and stay 35 ft from downed lines. Reply HELP for tips.")
    elif kind == "outage_confirmed":
        text = f"AEP: we've confirmed an outage affecting your home. {_range(est)} We'll text you if it changes."
    elif kind == "estimate_updated":
        prev = f" (was ~{previous_p50:g} hrs)" if previous_p50 is not None else ""
        text = f"AEP update: your restoration estimate changed{prev}. {_range(est)}"
    elif kind == "restored":
        text = ("AEP: power has been restored to your home. If you're still out, reply OUT or report it online. "
                "Thanks for your patience.")
    else:
        raise ValueError("kind must be one of pre_storm, outage_confirmed, estimate_updated, restored")
    if len(text) > SMS_LIMIT:
        text = text[: SMS_LIMIT - 1] + "…"
    return {"kind": kind, "text": text, "chars": len(text), "channel": "SMS", "source": "template"}
