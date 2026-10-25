"""AEP Operations dashboard: area-wide storm impact for internal staff (read-only).

Every asset's damage probability at the selected storm size (the same model and storm bridge the customer
side uses), rolled up per circuit, plus the public priority facilities (hospitals, fire stations, ambulance
services) linked to the circuit that most likely serves them. No customer-level data leaves this module:
customers appear only as counts per circuit, and the life-support / threat fields are never read.
"""
import math
from functools import lru_cache

import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree
from sqlalchemy import text

from ml.db import METRIC_CRS
from ml.score import risk_band
from restoration import ASSET_JOB

HIGH_RISK_MAIN_LINE = 0.2   # the app's "High" risk band (20%+) applied to a circuit's main line
SHIFT_HOURS = 16            # storm-mode crew shift used to size "crews to finish in 24 h"
TARGET_HOURS = (24, 72)
TOP_CIRCUITS = 10
TYPE_NAMES = {"pole": "poles", "primary": "primary lines", "secondary": "secondary lines", "transformer": "transformers",
              "fuse": "fuses", "recloser": "reclosers", "switch": "switches"}
FACILITY_NAMES = {"hospital": "Hospital", "fire_station": "Fire station", "ambulance": "Ambulance service"}
FACTOR_LABELS = {
    "pole_class_num": "weaker pole classes", "voltage_kv": "line voltage class", "line_usage": "main-line exposure",
    "wire_spec_top": "conductor type", "device_subtype": "device type", "overhead": "overhead construction",
    "material": "pole material", "bare_conductor": "bare conductor", "segment_len_m": "long overhead spans",
    "kva": "transformer size", "hist_asset_damaged": "repeat damage history", "age_years": "equipment age",
}


class OpsService:
    def __init__(self, engine, scorer, estimator):
        self.scorer, self.estimator = scorer, estimator
        a = scorer.assets
        self.assets = a
        self.to_ll = Transformer.from_crs(METRIC_CRS, 4326, always_xy=True)
        to_m = Transformer.from_crs(4326, METRIC_CRS, always_xy=True)
        self.circuit = a["circuit_key"].fillna("").astype(str).to_numpy()
        self.groups = {k: np.flatnonzero(self.circuit == k) for k in np.unique(self.circuit) if k}
        self.backbone = (a["line_usage"] == "Backbone").to_numpy()
        self.factor = a["factor_1"].fillna("").to_numpy()
        self.atype = a["asset_type"].fillna("").to_numpy()
        hours = {t: estimator.labor.mean_hours(job) for t, (job, _, _) in ASSET_JOB.items()}
        self.job_hours = a["asset_type"].map(hours).fillna(0).to_numpy()
        self.customers = scorer.customers.groupby("circuit_key").size().to_dict()

        cents = a.groupby("circuit_key")[["x", "y"]].mean()
        lon, lat = self.to_ll.transform(cents["x"].to_numpy(), cents["y"].to_numpy())
        self.centroids = {k: [round(float(x), 5), round(float(y), 5)] for k, x, y in zip(cents.index, lon, lat)}

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT facility_type, name, ST_X(geom), ST_Y(geom) FROM priority_facilities")).all()
        on_circuit = np.flatnonzero(self.circuit != "")
        tree = cKDTree(a[["x", "y"]].to_numpy()[on_circuit])
        self.facilities = []
        for ftype, name, lon_, lat_ in rows:
            x, y = to_m.transform(lon_, lat_)
            dist, i = tree.query([x, y])
            idx = on_circuit[i]
            self.facilities.append({"type": ftype, "type_label": FACILITY_NAMES.get(ftype, ftype), "name": name,
                                    "position": [round(lon_, 5), round(lat_, 5)],
                                    "circuit": self.circuit[idx], "distance_m": round(float(dist))})

    @lru_cache(maxsize=32)
    def _p(self, storm_n: float) -> np.ndarray:
        return self.scorer.p(self.assets, storm_n)

    def overview(self, storm: dict, crews: int | None = None) -> dict:
        p = self._p(round(float(storm["storm_n"]), 4))
        by = {}
        for k, m in self.groups.items():
            bb = m[self.backbone[m]]
            by[k] = {"expected_damage": float(p[m].sum()),
                     "main_line_p": float(1 - np.exp(np.log1p(-np.clip(p[bb], 0, 0.999999)).sum())) if bb.size else 0.0,
                     "crew_hours": float((p[m] * self.job_hours[m]).sum()),
                     "factor": self._top_factor(p, m), "driver": self._top_type(p, m)}
        crew_hours = float((p * self.job_hours).sum())
        expected_out = sum(self.customers.get(k, 0) * v["main_line_p"] for k, v in by.items())
        high = [k for k, v in by.items() if v["main_line_p"] >= HIGH_RISK_MAIN_LINE]
        crews_needed = {f"crews_for_{h}h": max(1, math.ceil(crew_hours / (SHIFT_HOURS * h / 24))) for h in TARGET_HOURS}
        fac_by_circuit = {}
        for f in self.facilities:
            fac_by_circuit.setdefault(f["circuit"], []).append(f)

        circuits = []
        for k, v in sorted(by.items(), key=lambda kv: -kv[1]["expected_damage"]):
            circuits.append({
                "circuit": k, "customers": int(self.customers.get(k, 0)),
                "expected_damage": round(v["expected_damage"], 1), "main_line_p": round(v["main_line_p"], 3),
                "risk_band": risk_band(v["main_line_p"]), "crew_hours": round(v["crew_hours"], 1),
                "top_factor": v["factor"], "top_equipment": v["driver"], "position": self.centroids.get(k),
                "facilities": [f["name"] for f in fac_by_circuit.get(k, [])],
            })
        facilities = []
        for f in self.facilities:
            c = by.get(f["circuit"], {"main_line_p": 0.0})
            band = risk_band(c["main_line_p"])
            facilities.append({**f, "risk_band": band, "main_line_p": round(c["main_line_p"], 3),
                               "action": self._facility_action(f["type"], band)})
        facilities.sort(key=lambda f: (-f["main_line_p"], f["type"] != "hospital", f["name"]))
        out = {
            "storm": storm,
            "kpis": {
                "expected_damaged_assets": round(float(p.sum()), 1),
                "expected_customers_out": int(round(expected_out)),
                "customers_high_risk_circuits": int(sum(self.customers.get(k, 0) for k in high)),
                "high_risk_circuits": len(high), "circuits": len(by),
                "crew_hours": round(crew_hours), **crews_needed,
                "facilities_at_risk": sum(1 for f in facilities if f["risk_band"] in ("High", "Very High")),
                "total_customers": int(sum(self.customers.values())),
            },
            "circuits": circuits[:TOP_CIRCUITS],
            "map": [{"circuit": c["circuit"], "position": c["position"], "main_line_p": c["main_line_p"],
                     "expected_damage": c["expected_damage"], "customers": c["customers"]}
                    for c in circuits if c["position"]],
            "facilities": facilities,
            "assumptions": [
                "Damage chance per asset comes from the same calibrated model and storm-size bridge as the customer view.",
                "A circuit's main-line chance is 1 - product(1 - p) over its backbone segments; expected customers out "
                "= customers on each circuit x that chance (step restoration and branch-line outages are not modeled).",
                "Crew-hours = expected damage x STORMS labor hours (incl. the 4.5x indirect-labor factor); crews to "
                f"finish within 24 h or 72 h assume {SHIFT_HOURS}-hour storm shifts per day.",
                "Facilities are public data (hospitals, fire, ambulance) linked to the circuit of the nearest asset.",
            ],
            "source": "model_estimate",
        }
        if crews:
            out["kpis"]["crews"] = crews
            out["kpis"]["hours_to_restore_all"] = round(crew_hours / crews, 1)
        return out

    def _top_factor(self, p: np.ndarray, mask: np.ndarray) -> str | None:
        f, w = self.factor[mask], p[mask]
        if not len(f) or w.sum() <= 0:
            return None
        names, inv = np.unique(f, return_inverse=True)
        best = names[np.bincount(inv, weights=w).argmax()]
        return FACTOR_LABELS.get(best, best.replace("_", " ")) if best else None

    def _top_type(self, p: np.ndarray, idx: np.ndarray) -> str | None:
        """Which equipment type carries most of the circuit's expected damage, e.g. 'poles (52%)'."""
        w = p[idx]
        if w.sum() <= 0:
            return None
        names, inv = np.unique(self.atype[idx], return_inverse=True)
        share = np.bincount(inv, weights=w) / w.sum()
        i = share.argmax()
        return f"{TYPE_NAMES.get(names[i], names[i])} ({share[i]:.0%})"

    @staticmethod
    def _facility_action(ftype: str, band: str) -> str:
        if band in ("High", "Very High"):
            return ("Call ahead: confirm backup generation and fuel; pre-stage a crew on this circuit"
                    if ftype == "hospital" else "Notify the station; prioritize this circuit in restoration order")
        if band == "Medium":
            return "Include in pre-storm outreach; confirm backup power status"
        return "Monitor; no pre-storm action needed"
