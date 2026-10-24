"""Live 7-day weather forecast -> storm size for the outage model.

The risk model learned how damage spreads across equipment for storms of a given *size*
(expected area damage reports); it has no weather inputs. This module bridges the two with a
documented, deliberately simple mapping from forecast wind gusts (plus ice, snow, thunderstorms
and heavy rain) to storm size. See ml/ASSUMPTIONS.md.

Source: Open-Meteo (free, no API key). Set FORECAST_FIXTURE=<json path> to use a fixed forecast
(tests, offline demos).
"""
import json
import math
import os
import threading
import time
from datetime import date

import httpx

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
DAYS = 7
CACHE_TTL_S = 3600
TIMEOUT_S = 8

# Peak wind gust (mph) -> expected damage reports across the area, log-interpolated.
# 40 mph ~ a typical minor event (12 reports); 58 mph (NWS severe) ~ the Sep 2024 storm (194);
# 70 mph+ ~ the Feb 2025 storm (1,254).
GUST_ANCHORS = [(20, 0.5), (30, 2.0), (40, 12.0), (50, 60.0), (58, 194.0), (70, 1254.0)]
STORM_N_CAP = 2000.0
ICE_CODES = {66, 67}            # freezing rain
THUNDER_CODES = {95, 96, 99}
REPLAYS = {"feb2025": ("the February 2025 storm", "high"), "sep2024": ("the September 2024 storm", "medium")}

WEATHER_TEXT = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers",
    82: "Heavy showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorms",
    96: "Thunderstorms with hail", 99: "Severe thunderstorms with hail",
}


def gust_to_storm_n(gust_mph: float) -> float:
    g = max(float(gust_mph or 0), 0.0)
    if g <= GUST_ANCHORS[0][0]:
        return GUST_ANCHORS[0][1] * max(g, 1.0) / GUST_ANCHORS[0][0]
    if g >= GUST_ANCHORS[-1][0]:
        return GUST_ANCHORS[-1][1]
    for (g0, n0), (g1, n1) in zip(GUST_ANCHORS, GUST_ANCHORS[1:]):
        if g <= g1:
            t = (g - g0) / (g1 - g0)
            return min(n1, math.exp(math.log(n0) + t * (math.log(n1) - math.log(n0))))  # min: float noise
    return GUST_ANCHORS[-1][1]


def day_storm(gust: float, precip_in: float, snow_in: float, code: int) -> tuple[float, list[str]]:
    """Storm size for one forecast day, plus the drivers that raised it."""
    n = gust_to_storm_n(gust)
    drivers = [f"wind gusts up to {gust:.0f} mph"]
    if code in ICE_CODES:
        n *= 4
        drivers.append("freezing rain (ice on lines and trees)")
    elif (snow_in or 0) >= 4:
        n *= 4
        drivers.append(f"{snow_in:.0f} in of snow")
    if code in THUNDER_CODES:
        n *= 2
        drivers.append("thunderstorms")
    if (precip_in or 0) >= 2:
        n *= 1.5
        drivers.append(f"{precip_in:.1f} in of rain")
    return min(n, STORM_N_CAP), drivers


def storm_label(n: float) -> str:
    if n < 2:
        return "Calm"
    if n < 12:
        return "Breezy"
    if n < 60:
        return "Windy"
    if n < 194:
        return "Storm"
    return "Severe storm"


class ForecastService:
    def __init__(self, levels: dict):
        self.levels = levels  # model severity anchors: {"high": {"n_records": ...}, ...}
        self._cache: dict = {}
        self._lock = threading.Lock()

    # ---- forecast ----

    def _fetch(self, lat: float, lon: float) -> dict:
        fixture = os.environ.get("FORECAST_FIXTURE")
        if fixture:
            return json.loads(open(fixture).read())
        params = {
            "latitude": round(lat, 3), "longitude": round(lon, 3), "forecast_days": DAYS,
            "daily": "wind_gusts_10m_max,wind_speed_10m_max,precipitation_sum,snowfall_sum,weather_code",
            "timezone": "America/New_York", "wind_speed_unit": "mph", "precipitation_unit": "inch",
        }
        r = httpx.get(OPEN_METEO, params=params, timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def forecast(self, lat: float, lon: float) -> dict:
        key = (round(lat, 2), round(lon, 2))  # ~1 km cell
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < CACHE_TTL_S:
                return hit[1]
        try:
            raw = self._fetch(lat, lon)["daily"]
            days = []
            for i, day in enumerate(raw["time"]):
                gust = raw["wind_gusts_10m_max"][i] or 0.0
                precip = raw["precipitation_sum"][i] or 0.0
                snow = raw["snowfall_sum"][i] or 0.0
                code = int(raw["weather_code"][i] or 0)
                n, drivers = day_storm(gust, precip, snow, code)
                d = date.fromisoformat(day)
                days.append({
                    "day": i, "date": day, "weekday": "Today" if i == 0 else d.strftime("%a"),
                    "gust_mph": round(gust, 1), "wind_mph": round(raw["wind_speed_10m_max"][i] or 0, 1),
                    "precip_in": round(precip, 2), "snow_in": round(snow, 1),
                    "conditions": WEATHER_TEXT.get(code, "Mixed"), "storm_n": round(n, 2),
                    "label": storm_label(n), "drivers": drivers,
                })
            out = {"available": True, "source": "Open-Meteo", "days": days}
        except Exception as e:  # network down / API change: degrade, never crash
            out = {"available": False, "source": "Open-Meteo", "error": e.__class__.__name__, "days": []}
        with self._lock:
            self._cache[key] = (time.time(), out)
        return out

    # ---- storm context ----

    def resolve(self, ctx: dict | None, lat: float, lon: float, pick_worst: bool = False) -> dict:
        """{"mode": "forecast", "day": 0..6} | {"mode": "replay", "storm": "feb2025"} -> storm details.

        pick_worst=True (and no explicit day) uses the riskiest forecast day in the next week.
        """
        ctx = dict(ctx or {"mode": "forecast"})
        if ctx.get("mode") == "replay":
            name = ctx.get("storm") or "feb2025"
            if name not in REPLAYS:
                raise ValueError(f"unknown replay storm '{name}'; use one of {sorted(REPLAYS)}")
            reference, level = REPLAYS[name]
            n = float(self.levels[level]["n_records"])
            return {"mode": "replay", "storm": name, "storm_n": n, "label": storm_label(n), "hypothetical": True,
                    "date": None, "lead_days": 0, "forecast_available": True,
                    "summary": f"a hypothetical storm like {reference}", "when": "in this hypothetical storm",
                    "drivers": [f"a storm the size of {reference}"]}
        fc = self.forecast(lat, lon)
        if not fc["available"] or not fc["days"]:
            return {"mode": "forecast", "storm_n": GUST_ANCHORS[0][1], "label": "Unknown", "hypothetical": False,
                    "date": None, "lead_days": 0, "forecast_available": False,
                    "summary": "no live forecast is available, so a calm day is assumed", "drivers": []}
        if "day" in ctx and ctx["day"] is not None:
            i = int(ctx["day"])
            if not 0 <= i < len(fc["days"]):
                raise ValueError(f"day must be between 0 and {len(fc['days']) - 1}")
            d = fc["days"][i]
        elif pick_worst:
            d = max(fc["days"], key=lambda x: x["storm_n"])
        else:
            d = fc["days"][0]
        when = "today" if d["day"] == 0 else f"{d['weekday']} {d['date'][5:]}"
        return {"mode": "forecast", "day": d["day"], "date": d["date"], "storm_n": d["storm_n"], "label": d["label"],
                "hypothetical": False, "lead_days": d["day"], "forecast_available": True,
                "summary": f"{d['label'].lower()} weather {when} ({d['conditions'].lower()}, gusts up to "
                           f"{d['gust_mph']:.0f} mph)", "drivers": [f"{x} {when}" for x in d["drivers"]],
                "when": when}
