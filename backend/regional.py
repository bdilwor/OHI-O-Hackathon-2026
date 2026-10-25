"""Regional estimates for locations outside AEP's challenge data ("stepping stone" mode).

The model needs a home's own equipment, connection and storm history, which only exist inside the challenge
area. Outside it we can still answer honestly: the live forecast works anywhere, and AEP's data tells us how
a storm of that size affects a *typical* home and repair. So a regional answer is the spread across a fixed
sample of AEP-area homes at the forecast storm size, always with Low confidence and clear labeling.

    python -m regional          # (re)build artifacts/regional_profile.json (~1-2 min; loaded by the API)

Location keys look like `region:<lat>,<lon>[,overhead|underground]`; they never contain an AEP premise ID.
"""
import json
import re
import threading
import time

import httpx
import numpy as np
from sqlalchemy import text

from ml.db import ARTIFACTS, METRIC_CRS
from ml.score import risk_band

PROFILE_PATH = ARTIFACTS / "regional_profile.json"
ANCHORS = [0.001, 0.01, 0.1, 0.5, 2.0, 12.0, 60.0, 194.0, 600.0, 1254.0, 2000.0]
SAMPLE_RISK = 400       # homes scored per storm size
SAMPLE_REPAIR = 24      # homes simulated per storm size (restoration Monte Carlo is ~150 ms each)
REF_CREWS, REF_DELAY = 2, 0.0  # restoration reference scenario; what-ifs rescale from it
IN_AREA_M = 150         # a point this close to a known service location should use the normal lookup
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "AEP-Outage-Concierge-HackOHIO-2026 (prototype)"
KEY_RE = re.compile(r"^region:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,(overhead|underground))?$")
SERVICES = ("all", "overhead", "underground")


def is_regional(premise: str | None) -> bool:
    return bool(premise) and str(premise).startswith("region:")


def parse_key(key: str) -> tuple[float, float, str]:
    m = KEY_RE.match(str(key))
    if not m:
        raise ValueError("regional location must look like region:<lat>,<lon>[,overhead|underground]")
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("latitude/longitude out of range")
    return lat, lon, m.group(3) or "all"


def make_key(lat: float, lon: float, service: str | None = None) -> str:
    return f"region:{lat:.3f},{lon:.3f}" + (f",{service}" if service in ("overhead", "underground") else "")


def _interp(n: float, xs: list[float], ys: list[float]) -> float:
    """Log-log interpolation in storm size (risk and repair time both grow roughly as a power of it)."""
    lx = np.log(np.clip(xs, 1e-6, None))
    ly = np.log(np.clip(ys, 1e-9, None))
    return float(np.exp(np.interp(np.log(max(n, 1e-6)), lx, ly)))


class RegionalService:
    def __init__(self, engine, scorer, estimator):
        self.engine, self.scorer, self.estimator = engine, scorer, estimator
        self._profile = json.loads(PROFILE_PATH.read_text()) if PROFILE_PATH.exists() else None
        self._lock = threading.Lock()
        self._geo_cache: dict = {}
        self._last_geo = 0.0
        from pyproj import Transformer
        self.to_metric = Transformer.from_crs(4326, METRIC_CRS, always_xy=True)

    # ---- profile (precomputed distribution across AEP-area homes) ----

    @property
    def profile(self) -> dict:
        if self._profile is None:
            with self._lock:
                if self._profile is None:
                    self._profile = build_profile(self.engine, self.scorer, self.estimator)
        return self._profile

    # ---- location ----

    def in_aep_area(self, lat: float, lon: float) -> bool:
        x, y = self.to_metric.transform(lon, lat)
        if not (np.isfinite(x) and np.isfinite(y)):
            return False
        dist, _ = self.scorer.customer_tree.query([x, y])
        return dist <= IN_AREA_M

    def geocode(self, address: str) -> dict | None:
        """OpenStreetMap Nominatim (free; cached; <= 1 request/second per its usage policy)."""
        q = address.strip()
        if q in self._geo_cache:
            return self._geo_cache[q]
        with self._lock:
            wait = 1.0 - (time.time() - self._last_geo)
            if wait > 0:
                time.sleep(wait)
            self._last_geo = time.time()
        r = httpx.get(NOMINATIM, params={"q": q, "format": "jsonv2", "limit": 1, "addressdetails": 1},
                      headers={"User-Agent": USER_AGENT}, timeout=10)
        r.raise_for_status()
        hits = r.json()
        out = None
        if hits:
            h = hits[0]
            a = h.get("address", {})
            place = a.get("city") or a.get("town") or a.get("village") or a.get("county") or ""
            label = ", ".join(p for p in (place, a.get("state")) if p) or h.get("display_name", q)[:60]
            out = {"lat": float(h["lat"]), "lon": float(h["lon"]), "label": label}
        self._geo_cache[q] = out
        return out

    def locate(self, address: str | None = None, lat: float | None = None, lon: float | None = None) -> dict:
        """A regional candidate for a place outside the data (masked: city/state or rounded coordinates)."""
        if address:
            try:
                g = self.geocode(address)
            except httpx.HTTPError:
                return {"found": False, "message": "We couldn't look up that address right now. Try tapping the "
                                                   "location on the map instead."}
            if not g:
                return {"found": False, "message": "We couldn't find that place. Try a city and state, or tap the map."}
            lat, lon, label = g["lat"], g["lon"], g["label"]
        elif lat is not None and lon is not None:
            label = f"Location near {abs(lat):.2f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.2f}° {'W' if lon < 0 else 'E'}"
        else:
            raise ValueError("Provide an address or lat/lon")
        if self.in_aep_area(lat, lon):
            return {"found": False, "in_aep_area": True,
                    "message": "This location is inside AEP's mapped area. Search your street address, or tap your "
                               "home on the map, for a home-specific estimate."}
        return {"found": True, "candidate": {
            "premise": make_key(lat, lon), "masked_address": label, "service_type": "unknown", "regional": True,
            "lat": round(lat, 3), "lon": round(lon, 3)},
            "message": "This location is outside AEP's challenge data, so we can give a regional estimate based on "
                       "similar AEP-area homes and the live forecast here."}

    # ---- estimates (same shapes as PremiseScorer.score / RestorationEstimator.estimate) ----

    def risk(self, key: str, storm: dict, model_version: str) -> dict:
        _, _, service = parse_key(key)
        prof = self.profile
        grp = prof["risk"][service if service in prof["risk"] else "all"]
        n = storm["storm_n"]
        q25, q50, q75 = (_interp(n, prof["anchors"], grp[q]) for q in ("p25", "p50", "p75"))
        who = {"overhead": "homes with overhead lines", "underground": "homes with underground lines"}.get(
            service, "homes")
        lo, hi = (f"{v * 100:.0f}%" if v >= 0.01 else "under 1%" for v in (q25, q75))
        return {
            "premise": key, "scope": "regional", "storm_n": round(n, 2),
            "risk_band": risk_band(q50), "probability": round(q50, 3), "probability_range": [round(q25, 3), round(q75, 3)],
            "confidence": "Low",
            "confidence_reason": "Confidence is low because this location is outside AEP's challenge data: we don't "
                                 "know the equipment, connection or storm history that serves it.",
            "top_factors": [
                f"In a storm this size, the middle half of AEP-area {who} had "
                + (f"an outage chance {lo}" if lo == hi else f"a {lo} to {hi} chance of an outage"),
                "The live forecast for this location sets the storm size",
                "Your local equipment, tree cover and storm history are unknown, so this is a regional typical value",
            ],
            "risk_breakdown": {},
            "assumptions": [
                f"Storm: {storm.get('summary', 'the forecast storm')}.",
                f"Regional estimate: the typical outage chance across {prof['n_risk']} sampled AEP-area {who} "
                "(Roanoke, VA region) at this storm size.",
                "Assumes this area's grid and vegetation behave like the AEP challenge area; terrain, tree cover, "
                "equipment age and the local utility's practices can differ.",
                "Another utility may serve this location; AEP data does not describe its grid.",
            ],
            "source": "regional_estimate",
            "disclaimer": "This is a regional model estimate for a location outside AEP's data, not a prediction for "
                          "this specific home.",
            "model_version": model_version, "fallback": False,
        }

    def restoration(self, key: str, storm: dict, scenario: dict | None = None) -> dict:
        parse_key(key)
        prof = self.profile
        sc = scenario or {}
        crews = int(sc.get("crews") or REF_CREWS)
        delay = float(sc.get("crew_arrival_delay_hrs") or 0.0)
        if crews < 1 or delay < 0:
            raise ValueError("crews must be >= 1 and crew_arrival_delay_hrs >= 0")
        n = storm["storm_n"]
        # Time = delay + crew-hours / crews (the estimator's model), rescaled from the reference scenario.
        work = {q: max(_interp(n, prof["anchors"], prof["repair"][q]) - REF_DELAY, 0.5) for q in ("p10", "p50", "p90")}
        hrs = {q: round((delay + work[q] * REF_CREWS / crews) * 2) / 2 for q in work}
        drivers = [f"Typical repair work for AEP-area homes in a storm this size, with {crews} crew"
                   f"{'s' if crews != 1 else ''} assigned" + (f" arriving after {delay:g} h" if delay else "")]
        if sc.get("extra_damage") or sc.get("access_blocked"):
            drivers.append("Specific damage and road access can't be modeled for a location outside AEP's data")
        return {
            "premise": key, "scope": "regional", "p10_hrs": hrs["p10"], "p50_hrs": hrs["p50"], "p90_hrs": hrs["p90"],
            "bottleneck_device": None, "drivers": drivers,
            "scenario": {"crews": crews, "crew_arrival_delay_hrs": delay, "storm_n": n},
            "assumptions": [
                f"Median repair range across {prof['n_repair']} simulated AEP-area homes at this storm size "
                "(STORMS labor hours x 4.5 indirect-labor factor, upstream-first repairs).",
                "Assumes local crews, travel and grid layout similar to the AEP challenge area.",
            ],
            "label": "MODEL_ESTIMATE", "fallback": False,
        }

    def map_payload(self, key: str, outage_p: float | None) -> dict:
        lat, lon, _ = parse_key(key)
        home = [lon, lat]
        return {"home": home, "radius_m": 1000, "regional": True, "label": "REGIONAL_ESTIMATE",
                "homes": [{"position": home, "outage_p": round(float(outage_p or 0), 4), "is_you": True}],
                "poles": [], "equipment": [], "lines": [], "supply_path": []}


def build_profile(engine, scorer, estimator, seed: int = 0) -> dict:
    """Outage-chance quantiles (by service type) and median repair ranges across sampled AEP-area homes."""
    with engine.connect() as conn:
        svc = dict(conn.execute(text("SELECT DISTINCT ON (premise_nu) premise_nu, kind_of_se FROM customers "
                                     "WHERE premise_nu IS NOT NULL")).all())
    ids = np.array(sorted(set(scorer.customers.index) & set(svc)))
    rng = np.random.default_rng(seed)
    risk_ids = rng.choice(ids, min(SAMPLE_RISK, len(ids)), replace=False)
    repair_ids = risk_ids[:SAMPLE_REPAIR]
    kind = np.array(["underground" if "under" in str(svc[p]).lower() else "overhead" for p in risk_ids])
    out = {"anchors": ANCHORS, "n_risk": int(len(risk_ids)), "n_repair": int(len(repair_ids)),
           "risk": {s: {"p25": [], "p50": [], "p75": []} for s in SERVICES},
           "repair": {"p10": [], "p50": [], "p90": []}, "model_version": scorer.bundle["model_version"],
           "service_counts": {s: int((kind == s).sum()) for s in ("overhead", "underground")}}
    for n in ANCHORS:
        probs = np.array([scorer.score(p, None, storm_n=n)["probability"] for p in risk_ids], float)
        for s in SERVICES:
            v = probs if s == "all" else probs[kind == s]
            v = v if len(v) else probs
            for q, key in ((25, "p25"), (50, "p50"), (75, "p75")):
                out["risk"][s][key].append(float(np.percentile(v, q)))
        ests = [estimator.estimate(p, {"storm_n": n, "crews": REF_CREWS, "crew_arrival_delay_hrs": REF_DELAY})
                for p in repair_ids]
        for q in ("p10", "p50", "p90"):
            vals = [e[f"{q}_hrs"] for e in ests if e.get(f"{q}_hrs") is not None]
            out["repair"][q].append(float(np.median(vals)) if vals else 1.0)
        print(f"  storm size {n:>7}: median outage chance {out['risk']['all']['p50'][-1]:.3f}, "
              f"median repair {out['repair']['p50'][-1]:.1f} h", flush=True)
    PROFILE_PATH.write_text(json.dumps(out, indent=1))
    return out


def main() -> None:
    from ml.db import get_engine
    from ml.score import PremiseScorer
    from restoration import RestorationEstimator

    engine = get_engine()
    scorer = PremiseScorer(engine)
    build_profile(engine, scorer, RestorationEstimator(engine, scorer))
    print(f"wrote {PROFILE_PATH}")


if __name__ == "__main__":
    main()
