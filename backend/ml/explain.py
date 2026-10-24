"""Model features -> plain-English sentences for customers.

Sentences describe *kinds* of equipment and aggregate history only: never asset IDs,
coordinates, circuit names, raw wire specs or anything from a customer record.
"""
from collections import Counter

LINE_USAGE = {"Backbone": "main-line", "Lateral": "branch-line", "Primary": "primary", "Service": "service"}
COMPONENTS = {
    "your_equipment": "the equipment that directly serves your home (transformer, fuses and service line)",
    "nearby": "power lines and poles within about 250 m of you",
    "main_line": "the main power line that feeds your neighborhood",
}


def _mode(values: list[str]) -> str | None:
    values = [v for v in values if v not in (None, "", "unknown", "nan")]
    return Counter(values).most_common(1)[0][0] if values else None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def factor_sentence(feature: str, values: list[str]) -> str:
    """One sentence for a risk-raising model factor shared by the most influential nearby assets."""
    v = _mode(values)
    f = _float(v)
    if feature == "voltage_kv" and f:
        return f"Your power is carried on {f:g} kV distribution lines, a line class that saw more damage than average in past storms."
    if feature == "pole_class_num" and f:
        return f"Many poles near you are class {f:g} poles; this strength class was damaged more often than average in past storms."
    if feature == "line_usage" and v:
        return f"Your area relies on overhead {LINE_USAGE.get(v, v.lower())} lines, which were damaged more often than average in past storms."
    if feature == "overhead":
        return "Lines near you are overhead, so they are exposed to wind, ice and falling trees."
    if feature == "material" and v:
        return f"Poles near you are mostly {v.lower()} poles, a material that was damaged more often than average in past storms."
    if feature == "device_subtype" and v:
        return f"Equipment serving you is {v.lower()}-type equipment, which was damaged more often than average in past storms."
    if feature == "bare_conductor":
        return "Some nearby lines use bare (uninsulated) overhead wire, which is more exposed to tree contact."
    if feature == "wire_spec_top":
        return "The type of wire used on lines near you was linked to higher damage rates in past storms."
    if feature == "segment_len_m":
        return "Long stretches of line serve your area, so more line is exposed to the storm."
    if feature == "age_years" and f:
        return f"Equipment near you is roughly {f:.0f} years old; older equipment was damaged more often in past storms."
    if feature == "height_ft" and f:
        return f"Poles near you are about {f:.0f} ft tall; this pole height was linked to higher damage rates in past storms."
    if feature in ("kva", "fuse_size_a"):
        return "The size of the equipment serving your street was linked to higher damage rates in past storms."
    if feature == "customers_downstream":
        return "Equipment serving you also feeds many other homes, so it sits on a longer, more exposed part of the network."
    if feature in ("n_upline_devices", "n_upline_reclosers"):
        return "Your power travels through several protective devices on the way to you, which adds exposure."
    if feature in ("hist_share_250m", "hist_share_500m"):
        return "Your immediate area has had a larger-than-average share of past storm damage."
    if feature == "hist_circuit_share":
        return "The line that serves you has had a larger-than-average share of past storm damage."
    if feature == "hist_asset_damaged":
        return "Equipment that serves you has been damaged in a previous storm."
    if feature == "hist_access_rate_500m":
        return "Past repairs near you often involved hard-to-reach locations."
    return "Characteristics of the equipment in your area were linked to higher damage rates in past storms."


def component_sentence(component: str, share: float) -> str:
    return f"About {share:.0%} of the estimated risk comes from {COMPONENTS[component]}."


def history_sentence(n_nearby: int) -> str:
    if n_nearby == 0:
        return "There are no recorded storm damage reports within about 500 m of you in our historical data."
    return f"There were {n_nearby} storm damage report{'s' if n_nearby != 1 else ''} within about 500 m of you in past storms."
