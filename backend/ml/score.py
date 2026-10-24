"""Premise-level outage risk, the engine behind the chatbot's `get_outage_risk` tool.

    python -m ml.score 551102222 --severity high
    python -m ml.score --lat 37.27 --lon -79.94

Load PremiseScorer once at startup (it builds the topology graph and spatial indexes),
then call .score() per request. Output contains no IDs, coordinates or private fields.
"""
import argparse
import json

import networkx as nx
import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.spatial import cKDTree
from sqlalchemy.engine import Engine

from . import explain
from .db import METRIC_CRS, get_engine, read_frame
from .model import load_bundle

DEVICE_TYPES = ("transformer", "fuse", "recloser", "switch")
NEARBY_RADIUS_M = 250
SERVICE_SNAP_M = 60
HISTORY_RADIUS_M = 500
LOCATION_SNAP_M = 150
BANDS = [(0.05, "Low"), (0.20, "Medium"), (0.50, "High"), (1.01, "Very High")]
CONF_HIGH_MIN_RECORDS = 20
CONF_MEDIUM_MIN_RECORDS = 5
FACTOR_RANK_WEIGHTS = (1.0, 0.6, 0.3)


def str_columns(cols: list[str]) -> dict:
    """IDs like premise 032205817 or location 37800343D00129 must stay strings (keep leading zeros)."""
    return {c: str for c in cols}


def build_topology_graph(hops: pd.DataFrame) -> nx.DiGraph:
    """premise -> transformer -> fuses/reclosers -> breaker, from rows sorted by (premise_nu, hop_seq)."""
    nxt = hops.groupby("premise_nu", sort=False)["location_n"].shift(-1)
    first = hops["premise_nu"] != hops["premise_nu"].shift()
    g = nx.DiGraph()
    g.add_edges_from(zip(zip(["premise"] * first.sum(), hops.loc[first, "premise_nu"]),
                         zip(["device"] * first.sum(), hops.loc[first, "location_n"])))
    chain = nxt.notna()
    g.add_edges_from(zip(zip(["device"] * chain.sum(), hops.loc[chain, "location_n"]),
                         zip(["device"] * chain.sum(), nxt[chain])))
    return g


def risk_band(p: float) -> str:
    return next(label for upper, label in BANDS if p < upper)


class PremiseScorer:
    def __init__(self, engine: Engine | None = None):
        engine = engine or get_engine()
        self.bundle = load_bundle()
        self.levels = self.bundle["severity_levels"]

        factor_cols = ", ".join(f"factor_{k}, factor_{k}_value" for k in (1, 2, 3))
        self.assets = read_frame(
            f"SELECT asset_uid, asset_type, location_n, circuit_key, line_usage, x, y, p_low, p_medium, p_high, "
            f"{factor_cols} FROM asset_risk", engine, dtype=str_columns(["location_n", "factor_1_value",
                                                                       "factor_2_value", "factor_3_value"]))
        for col in self.assets.select_dtypes(include="object").columns:  # SQL NULL -> None, as read_sql gave
            self.assets[col] = self.assets[col].astype(object).where(self.assets[col].notna(), None)
        self.devices = self.assets[self.assets["asset_type"].isin(DEVICE_TYPES)].set_index("location_n")
        self.devices = self.devices[~self.devices.index.duplicated()]
        self.local = self.assets[self.assets["asset_type"].isin(["primary", "pole"])].reset_index(drop=True)
        self.local_tree = cKDTree(self.local[["x", "y"]].to_numpy())
        self.secondary = self.assets[self.assets["asset_type"] == "secondary"].reset_index(drop=True)
        self.secondary_tree = cKDTree(self.secondary[["x", "y"]].to_numpy())
        self.backbone = {ck: g for ck, g in self.assets[self.assets["line_usage"] == "Backbone"].groupby("circuit_key")}

        # Customers: only routing fields + location (never critical_c / esri_defau, which live in customer_private).
        cust = read_frame(
            f"""SELECT DISTINCT ON (premise_nu) premise_nu, circuit_key, aep_xfmr_l,
                       ST_X(ST_Transform(geom, {METRIC_CRS})) AS x, ST_Y(ST_Transform(geom, {METRIC_CRS})) AS y
                FROM customers WHERE premise_nu IS NOT NULL ORDER BY premise_nu""",
            engine, dtype=str_columns(["premise_nu", "aep_xfmr_l"]),
        )
        self.customers = cust.set_index("premise_nu")
        self.customer_tree = cKDTree(cust[["x", "y"]].to_numpy())
        self.customer_ids = cust["premise_nu"].to_numpy()

        arcos = pd.read_sql(
            f"SELECT circuit_key, ST_X(ST_Transform(geom, {METRIC_CRS})) AS x, "
            f"ST_Y(ST_Transform(geom, {METRIC_CRS})) AS y FROM arcos_damage", engine)
        self.arcos_tree = cKDTree(arcos[["x", "y"]].to_numpy())
        self.circuit_records = arcos["circuit_key"].value_counts().to_dict()
        self.n_arcos = len(arcos)

        hops = read_frame("SELECT premise_nu, hop_seq, location_n FROM premise_upline ORDER BY premise_nu, hop_seq",
                          engine, dtype=str_columns(["premise_nu", "location_n"]))
        self.graph = build_topology_graph(hops)
        self.to_metric = Transformer.from_crs(4326, METRIC_CRS, always_xy=True)

    # ---- public API -------------------------------------------------------------------------

    def score(self, premise: str, severity: str = "medium") -> dict:
        if severity not in self.levels:
            raise ValueError(f"severity must be one of {list(self.levels)}")
        premise = str(premise).strip()
        if premise not in self.customers.index:
            return self._fallback(premise, severity, "We couldn't find this service location in our records.")
        cust = self.customers.loc[premise]
        in_topology = self.graph.has_node(("premise", premise))
        parts = self.exposure(premise, cust, in_topology)
        if not any(len(df) for df in parts.values()):
            return self._fallback(premise, severity, "There isn't enough network information near this location.")
        return self._result(premise, cust, severity, parts, in_topology)

    def score_location(self, lat: float, lon: float, severity: str = "medium") -> dict:
        x, y = self.to_metric.transform(lon, lat)
        if not (np.isfinite(x) and np.isfinite(y)):  # far outside the service area's UTM zone
            return self._fallback(None, severity, "This map location is outside the area we cover.")
        dist, idx = self.customer_tree.query([x, y])
        if dist > LOCATION_SNAP_M:
            return self._fallback(None, severity, "This map location isn't close enough to a service location we know.")
        result = self.score(self.customer_ids[idx], severity)
        result["premise"] = None  # caller gave a map point; don't reveal which premise it snapped to
        result["assumptions"].append(f"The map point was matched to the nearest service location (within {LOCATION_SNAP_M} m).")
        return result

    # ---- internals ----------------------------------------------------------------------------

    def exposure(self, premise: str, cust: pd.Series, in_topology: bool) -> dict[str, pd.DataFrame]:
        upline = {loc for kind, loc in nx.descendants(self.graph, ("premise", premise)) if kind == "device"} \
            if in_topology else {cust["aep_xfmr_l"]}
        own = self.devices.loc[self.devices.index.intersection(list(upline))].reset_index()
        dist, idx = self.secondary_tree.query([cust["x"], cust["y"]])
        if dist <= SERVICE_SNAP_M:
            own = pd.concat([own, self.secondary.iloc[[idx]]], ignore_index=True)

        anchor = (self.devices.loc[cust["aep_xfmr_l"], ["x", "y"]].to_numpy(dtype=float)
                  if cust["aep_xfmr_l"] in self.devices.index else np.array([cust["x"], cust["y"]]))
        near = self.local.iloc[self.local_tree.query_ball_point(anchor, NEARBY_RADIUS_M)]
        if pd.notna(cust["circuit_key"]):
            near = near[near["circuit_key"].isna() | (near["circuit_key"] == cust["circuit_key"])]
        main = self.backbone.get(cust["circuit_key"], self.assets.iloc[0:0])

        # Each asset counts once, in the most specific component.
        seen = set(own["asset_uid"])
        near = near[~near["asset_uid"].isin(seen)]
        seen |= set(near["asset_uid"])
        main = main[~main["asset_uid"].isin(seen)]
        return {"your_equipment": own, "nearby": near, "main_line": main}

    def _result(self, premise, cust, severity, parts, in_topology) -> dict:
        col = f"p_{severity}"
        survive = {k: float(np.prod(1 - df[col].to_numpy())) for k, df in parts.items()}
        prob = 1 - float(np.prod(list(survive.values())))
        comp_risk = {k: 1 - s for k, s in survive.items()}

        n_circuit = int(self.circuit_records.get(cust["circuit_key"], 0))
        if n_circuit >= CONF_HIGH_MIN_RECORDS and in_topology:
            confidence, why = "High", f"the line that serves you has {n_circuit} historical damage records"
        elif n_circuit >= CONF_MEDIUM_MIN_RECORDS:
            confidence, why = "Medium", f"the line that serves you has only {n_circuit} historical damage records"
        else:
            confidence, why = "Low", "there is little storm history for the line that serves you"
        if not in_topology:
            why += ", and your exact network connection isn't in our data"

        factors = []
        top_comp = max(comp_risk, key=comp_risk.get)
        if sum(comp_risk.values()) > 0:
            factors.append(explain.component_sentence(top_comp, comp_risk[top_comp] / sum(comp_risk.values())))
        factors += self._model_factors(pd.concat(parts.values(), ignore_index=True), col)
        n_nearby = len(self.arcos_tree.query_ball_point([cust["x"], cust["y"]], HISTORY_RADIUS_M))
        factors.append(explain.history_sentence(n_nearby))

        level = self.levels[severity]
        return {
            "premise": premise,
            "severity": severity,
            "risk_band": risk_band(prob),
            "probability": round(prob, 3),
            "confidence": confidence,
            "confidence_reason": f"Confidence is {confidence.lower()} because {why}.",
            "top_factors": factors,
            "risk_breakdown": {k: round(v, 3) for k, v in comp_risk.items()},
            "assumptions": [
                f"Storm scenario '{severity}' is modeled on {level['reference']}; no live weather forecast is used.",
                f"Based on {self.n_arcos:,} damage records from past storms in this area (2022-2026).",
                "Counts damage to equipment on your supply path, lines and poles within about 250 m, and the main "
                "line feeding your neighborhood; equipment failures are treated as independent.",
                "Any interruption counts, including short ones that crews or automatic devices restore quickly.",
            ],
            "source": "model_estimate",
            "disclaimer": "This is a model estimate, not a confirmed outage or restoration time from the utility.",
            "model_version": self.bundle["model_version"],
            "fallback": False,
        }

    def _model_factors(self, exposure: pd.DataFrame, col: str, n: int = 2) -> list[str]:
        top = exposure.nlargest(15, col)
        weights, values = {}, {}
        for k, w_rank in enumerate(FACTOR_RANK_WEIGHTS, start=1):
            for feat, val, p in zip(top[f"factor_{k}"], top[f"factor_{k}_value"], top[col]):
                if feat:
                    weights[feat] = weights.get(feat, 0.0) + w_rank * p
                    values.setdefault(feat, []).append(val)
        best = sorted(weights, key=weights.get, reverse=True)[:n]
        return [explain.factor_sentence(f, values[f]) for f in best]

    def _fallback(self, premise, severity, reason: str) -> dict:
        return {
            "premise": premise,
            "severity": severity,
            "risk_band": "Unknown",
            "probability": None,
            "confidence": "Low",
            "confidence_reason": reason,
            "top_factors": [],
            "assumptions": ["No reliable location-specific estimate is possible, so general storm guidance applies."],
            "message": f"{reason} We can't give a reliable outage estimate for this location, so please follow "
                       "general storm preparation guidance and report any outage you experience.",
            "source": "fallback",
            "model_version": self.bundle["model_version"],
            "fallback": True,
        }


def main() -> None:
    ap = argparse.ArgumentParser(description="Premise outage-risk estimate")
    ap.add_argument("premise", nargs="?")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--severity", default="medium", choices=["low", "medium", "high"])
    args = ap.parse_args()
    scorer = PremiseScorer()
    if args.premise:
        out = scorer.score(args.premise, args.severity)
    elif args.lat is not None and args.lon is not None:
        out = scorer.score_location(args.lat, args.lon, args.severity)
    else:
        ap.error("give a premise number or --lat/--lon")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
