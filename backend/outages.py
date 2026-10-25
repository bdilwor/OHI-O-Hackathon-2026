"""Outage details: where it is, what area it affects, why it takes X hours, reported time and estimated fix.

Two sources share one record shape:
  - current(premise): the outage feed (in this project, the scripted demo timeline for the demo home)
  - test(...):        the "Outage test" tab: a simulated fault on a chosen or clicked piece of equipment

Stateless and read-only: nothing is stored or written to the database. The affected area is computed
from the real grid topology (every premise downstream of the faulted device); repair time comes from
the restoration estimator (STORMS labor hours + Monte Carlo) with the fault's damage confirmed.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import networkx as nx
import numpy as np
from pyproj import Transformer
from scipy.spatial import ConvexHull, cKDTree
from sqlalchemy import text

from ml.db import METRIC_CRS
from restoration import DAMAGE_KEYS, DEVICE_NAMES

LOCAL_TZ = ZoneInfo("America/New_York")
SCENE_RADIUS_M = 1000      # affected-home positions are only sent for the map scene around the home
CLICK_SNAP_M = 40          # a map click must be this close to a piece of equipment
PROTECTIVE_SNAP_M = 400    # a pole/line fault trips the nearest fuse/recloser on its circuit within this distance
CALM_STORM_N = 0.5         # outage tests assume ordinary weather around the fault
TRAVEL_HRS = 1.0           # first crew arrives about an hour after the report (calm day)
DEVICE_TYPES = ("transformer", "fuse", "recloser", "switch")
KIND_NAMES = {"transformer": "Transformer", "fuse": "Fuse", "recloser": "Recloser", "switch": "Switch"}
SUBTYPE_KIND = {"Distribution": "transformer", "Fuse": "fuse", "Recloser (Hydraulic)": "recloser",
                "Recloser (Electronic)": "recloser", "Sectionalizer": "recloser"}

# Outage causes -> the damage crews must repair (restoration job types).
CAUSES = {
    "tree": ("Tree on the power line", {"primary_wire": 1}),
    "pole": ("Broken utility pole", {"pole": 1, "primary_wire": 1}),
    "vehicle": ("Vehicle hit a utility pole", {"pole": 1}),
    "transformer": ("Transformer failure", {"transformer": 1}),
    "lightning": ("Lightning strike", {"fuse": 1}),
    "equipment": ("Equipment failure", None),  # the faulted device itself
}
JOB_WORDS = {"pole": "replace a utility pole", "primary_wire": "splice and re-attach a damaged power line",
             "secondary_wire": "repair a street-level wire", "service": "repair a service line",
             "fuse": "replace a blown fuse", "transformer": "replace a transformer", "recloser": "repair a recloser",
             "switch": "repair a switch"}


def fmt_time(dt: datetime) -> str:
    """Day, month, year: 'Sat, 24 Oct 2026, 2:10 PM' (local time)."""
    local = dt.astimezone(LOCAL_TZ)
    return local.strftime("%a, %d %b %Y, ") + local.strftime("%I:%M %p").lstrip("0")


class OutageService:
    def __init__(self, engine, scorer, estimator, mapdata, storm):
        self.engine = engine
        self.scorer = scorer
        self.estimator = estimator
        self.mapdata = mapdata
        self.storm = storm
        self.assets = scorer.assets
        self.asset_tree = cKDTree(self.assets[["x", "y"]].to_numpy())
        self.to_wgs = Transformer.from_crs(METRIC_CRS, 4326, always_xy=True)
        self.to_metric = Transformer.from_crs(4326, METRIC_CRS, always_xy=True)
        with engine.connect() as conn:
            self.road = dict(conn.execute(text(
                "SELECT DISTINCT ON (premise_nu) premise_nu, road_name FROM customers "
                "WHERE premise_nu IS NOT NULL ORDER BY premise_nu")).all())
        self._downstream: dict = {}

    # ---- equipment ----

    def path_devices(self, premise: str) -> list[dict]:
        """Equipment on the home's supply path (transformer -> fuses -> reclosers) for the picker."""
        self._check(premise)
        hops = self.mapdata._supply_path(premise, [0, 0], CALM_STORM_N)[1:]  # skip the home node
        devices = []
        for i, node in enumerate(hops):
            kind = SUBTYPE_KIND.get(node["kind"])
            if kind:
                devices.append({"index": i, "kind": kind, "label": node["label"], "position": node["position"]})
        return devices

    def _path_locations(self, premise: str) -> list[str]:
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT location_n FROM premise_upline WHERE premise_nu = :p ORDER BY hop_seq"),
                                {"p": premise}).scalars().all()

    def resolve_fault(self, premise: str, device: int | None = None, lat: float | None = None,
                      lon: float | None = None) -> dict:
        """-> {location, kind, label, x, y, clicked_kind}. Map clicks are resolved here; the browser never sees IDs."""
        self._check(premise)
        devs = self.scorer.devices
        if lat is not None and lon is not None:
            x, y = self.to_metric.transform(lon, lat)
            if not (np.isfinite(x) and np.isfinite(y)):
                raise ValueError("That point is outside the area we cover.")
            dist, idx = self.asset_tree.query([x, y])
            if dist > CLICK_SNAP_M:
                raise ValueError("Click closer to a pole, wire, transformer or fuse.")
            hit = self.assets.iloc[idx]
            if hit["asset_type"] in DEVICE_TYPES and self.scorer.graph.has_node(("device", hit["location_n"])):
                loc, clicked = hit["location_n"], hit["asset_type"]
            else:  # pole or wire: the nearest protective device on the same circuit operates for the fault
                cand = devs[devs["asset_type"].isin(["fuse", "recloser"]) & (devs["circuit_key"] == hit["circuit_key"])]
                cand = cand[[self.scorer.graph.has_node(("device", c)) for c in cand.index]]
                if cand.empty:
                    raise ValueError("No fuse or recloser protects that spot in our data; try another point.")
                d = np.hypot(cand["x"] - hit["x"], cand["y"] - hit["y"])
                if d.min() > PROTECTIVE_SNAP_M:
                    raise ValueError("No fuse or recloser close enough to that spot; try another point.")
                loc, clicked = d.idxmin(), "pole" if hit["asset_type"] == "pole" else "line"
            fx, fy = float(hit["x"]), float(hit["y"])
        else:
            locs = [loc for loc in self._path_locations(premise) if loc in devs.index]
            i = int(device or 0)
            if not 0 <= i < len(locs):
                raise ValueError(f"device must be between 0 and {len(locs) - 1}")
            loc = locs[i]
            fx, fy, clicked = float(devs.loc[loc, "x"]), float(devs.loc[loc, "y"]), None
        kind = devs.loc[loc, "asset_type"]
        return {"location": loc, "kind": kind, "x": fx, "y": fy, "clicked_kind": clicked,
                "device_x": float(devs.loc[loc, "x"]), "device_y": float(devs.loc[loc, "y"])}

    def downstream(self, location: str) -> list[str]:
        """Every premise fed through this device, from the real topology graph (cached)."""
        if location not in self._downstream:
            nodes = nx.ancestors(self.scorer.graph, ("device", location))
            self._downstream[location] = sorted(p for k, p in nodes if k == "premise")
        return self._downstream[location]

    # ---- the outage record ----

    def _lonlat(self, x, y) -> list[float]:
        lon, lat = self.to_wgs.transform(float(x), float(y))
        return [round(lon, 6), round(lat, 6)]

    def _area(self, premise: str, affected: list[str]) -> dict:
        cust = self.scorer.customers.loc[[p for p in affected if p in self.scorer.customers.index]]
        xy = cust[["x", "y"]].to_numpy(dtype=float)
        hx, hy = (float(v) for v in self.scorer.customers.loc[premise, ["x", "y"]])
        near = xy[np.hypot(xy[:, 0] - hx, xy[:, 1] - hy) <= SCENE_RADIUS_M] if len(xy) else xy
        if len(near):
            _, keep = np.unique(np.round(near, 0), axis=0, return_index=True)  # one per building
            near = near[keep]
        polygon, area_km2 = [], 0.0
        if len(xy) >= 3:
            try:
                hull = ConvexHull(xy)
                ring = xy[hull.vertices]
                cx, cy = ring.mean(axis=0)
                ring = np.column_stack([cx + (ring[:, 0] - cx) * 1.08 + np.sign(ring[:, 0] - cx) * 25,
                                        cy + (ring[:, 1] - cy) * 1.08 + np.sign(ring[:, 1] - cy) * 25])  # pad
                polygon = [self._lonlat(x, y) for x, y in ring]
                area_km2 = hull.volume / 1e6  # 2-D "volume" is the area
            except Exception:  # collinear points
                polygon = []
        if not polygon and len(xy):  # one or two homes: a small circle around them
            cx, cy = xy.mean(axis=0)
            polygon = [self._lonlat(cx + 45 * np.cos(a), cy + 45 * np.sin(a)) for a in np.linspace(0, 2 * np.pi, 16)]
        streets = Counter(self.road.get(p) for p in affected if self.road.get(p))
        return {
            "homes": len(affected),
            "area_km2": round(area_km2, 3),
            "streets": [s.title() for s, _ in streets.most_common(3)],
            "polygon": polygon,
            "positions": [self._lonlat(x, y) for x, y in near],
        }

    def build(self, premise: str, fault: dict, cause: str, reported_at: datetime, crews: int,
              damage: dict | None = None, cause_label: str | None = None, delay_hrs: float = TRAVEL_HRS,
              storm_n: float = CALM_STORM_N, source: str = "test") -> dict:
        if cause not in CAUSES:
            raise ValueError(f"cause must be one of {sorted(CAUSES)}")
        label, cause_damage = CAUSES[cause]
        damage = damage or cause_damage or {fault["kind"]: 1}
        affected = self.downstream(fault["location"])
        you = premise in set(affected)
        target = premise if you else (affected[0] if affected else premise)
        est = self.estimator.estimate(target, {"crews": crews, "crew_arrival_delay_hrs": delay_hrs, "storm_n": storm_n},
                                      confirmed={k: v for k, v in damage.items()})
        hours_ago = max(0.0, (datetime.now(timezone.utc) - reported_at).total_seconds() / 3600)
        p10, p50, p90 = est["p10_hrs"], est["p50_hrs"], est["p90_hrs"]
        etr = {k: reported_at + timedelta(hours=h) for k, h in (("p10", p10), ("p50", p50), ("p90", p90))}

        device_name = DEVICE_NAMES.get({"transformer": "Distribution", "fuse": "Fuse"}.get(fault["kind"], ""),
                                       f"a {KIND_NAMES.get(fault['kind'], 'device').lower()} on your line")
        if not you:
            device_name = device_name.replace("your street", "the street").replace("your branch line", "a branch line") \
                .replace("your line", "a nearby line")
        area = self._area(premise, affected)
        near = f", near {area['streets'][0]}" if area["streets"] else ""
        clicked = {"pole": "A pole on the line protected by ", "line": "The power line protected by "}
        where = f"{clicked.get(fault.get('clicked_kind'), '')}{device_name}{near}"
        where = where[0].upper() + where[1:]

        labor = self.estimator.labor
        work = sum(labor.mean_hours(job) * n for job, n in damage.items() if job in JOB_WORDS)
        repairs = " and ".join(f"{JOB_WORDS[j]}" + (f" (x{n})" if n > 1 else "") for j, n in damage.items() if j in JOB_WORDS)
        why = [
            f"Crews need to {repairs}: about {work:.0f} crew-hours of work, using AEP's standard labor times.",
            f"{crews} crew{'s' if crews != 1 else ''} assigned; the first is expected about {delay_hrs:g} h after the report.",
            f"Repairs go upstream first; your power returns once {est['bottleneck_device']} is fixed."
            if est.get("bottleneck_device") else "Repairs go upstream first.",
            "Times include travel, safety checks and setup (AEP's 4.5x indirect-labor factor).",
        ]
        if area["homes"] > 50:
            why.append(f"{area['homes']} homes are affected, so crews may restore larger sections first.")

        return {
            "source": source,
            "status": "in_progress",
            "cause": cause_label or label,
            "location": where,
            "fault_position": self._lonlat(fault["x"], fault["y"]),
            "device_position": self._lonlat(fault["device_x"], fault["device_y"]),
            "affects_you": you,
            "affected": area,
            "reported_at": reported_at.isoformat(timespec="minutes"),
            "reported_display": fmt_time(reported_at),
            "etr": {k: v.isoformat(timespec="minutes") for k, v in etr.items()},
            "etr_display": {k: fmt_time(v) for k, v in etr.items()},
            "hours": {"p10": p10, "p50": p50, "p90": p90, "elapsed": round(hours_ago, 1),
                      "remaining": round(max(0.0, p50 - hours_ago), 1)},
            "crews": crews,
            "bottleneck": est.get("bottleneck_device"),
            "why": why,
            "label": "MODEL_ESTIMATE",
        }

    # ---- sources ----

    def test(self, premise: str, cause: str = "tree", device: int | None = None, lat: float | None = None,
             lon: float | None = None, reported_hours_ago: float = 0.0, crews: int = 2) -> dict:
        if not 0 <= reported_hours_ago <= 72 or not 1 <= crews <= 30:
            raise ValueError("reported_hours_ago 0-72, crews 1-30")
        fault = self.resolve_fault(premise, device, lat, lon)
        reported = datetime.now(timezone.utc) - timedelta(hours=reported_hours_ago)
        return self.build(premise, fault, cause, reported, crews, source="test")

    def current(self, premise: str) -> dict | None:
        """The outage feed for this home: the demo timeline's outage when it applies, else None."""
        self._check(premise)
        st = self.storm
        if not st.applies_to(premise) or not (st.outage_for(premise) or st.restored_for(premise)):
            return None
        devices = self.path_devices(premise)
        idx = next((d["index"] for d in devices if d["kind"] == "fuse"), devices[0]["index"] if devices else 0)
        fault = self.resolve_fault(premise, idx)
        scenario = st.scenario()
        hours_out = st.current.get("hours_since_outage") or 0
        damage = {k: v for k, v in (st.confirmed_damage(premise) or {"wire": 1}).items()}
        jobs = {DAMAGE_KEYS.get(k.lower(), k): v for k, v in damage.items()}
        parts = ", ".join(f"{v} {k.lower()}" for k, v in damage.items())
        record = self.build(premise, fault, "equipment", datetime.now(timezone.utc) - timedelta(hours=hours_out),
                            scenario.get("crews", 4), damage=jobs, cause_label=f"Storm damage ({parts})",
                            delay_hrs=scenario.get("crew_arrival_delay_hrs", TRAVEL_HRS),
                            storm_n=self.scorer.storm_size(scenario.get("storm_severity", "high")), source="feed")
        if st.restored_for(premise):
            record["status"] = "restored"
        return record

    def _check(self, premise: str) -> None:
        if premise not in self.scorer.customers.index:
            raise LookupError("We couldn't find this service location in our records.")

