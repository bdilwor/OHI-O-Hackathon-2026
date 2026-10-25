"""Map payload for the frontend's 2D/3D map: the model's prediction around one home for one storm.

Everything is generic and ID-free: homes are service-point positions with an *area* outage chance,
equipment carries only a type and a damage chance, lines carry a damage chance and their height.
This payload goes to the customer's own browser only; it is never sent to the LLM.
"""
import json

import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree
from sqlalchemy import text

from ml.db import METRIC_CRS
from restoration import DEVICE_NAMES

RADIUS_M = 1000           # scene radius around the home
SECONDARY_RADIUS_M = 600  # street-level wires are dense; keep the scene light
HOME_AREA_M = 150         # a home's area outage chance counts equipment within this distance
OVERHEAD_Z = {"primary": 9.0, "secondary": 7.0}  # wire heights (m) for the 3D view
EQUIPMENT = {"transformer": "Transformer", "fuse": "Fuse", "recloser": "Recloser", "switch": "Switch"}
SIMPLIFY_DEG = 0.00002


class MapData:
    def __init__(self, engine, scorer):
        self.engine = engine
        self.scorer = scorer
        self.assets = scorer.assets
        self.asset_tree = cKDTree(self.assets[["x", "y"]].to_numpy())
        self.row_by_uid = {uid: i for i, uid in enumerate(self.assets["asset_uid"])}
        self.to_wgs = Transformer.from_crs(METRIC_CRS, 4326, always_xy=True)
        self._line_cache: dict = {}  # home -> line geometry (never changes; only risk is per storm)
        self._path_cache: dict = {}  # premise -> upline hops

    def _lonlat(self, x, y) -> np.ndarray:
        x, y = np.atleast_1d(np.asarray(x, float)), np.atleast_1d(np.asarray(y, float))
        if x.size == 1:  # pyproj routes 1-element arrays through its scalar path (NumPy deprecation)
            lon, lat = self.to_wgs.transform(float(x[0]), float(y[0]))
            return np.round(np.array([[lon, lat]]), 6)
        lon, lat = self.to_wgs.transform(x, y)
        return np.round(np.column_stack([lon, lat]), 6)

    def context(self, premise: str, storm_n: float, home_outage_p: float | None = None,
                area: tuple | None = None) -> dict | None:
        premise = str(premise).strip()
        if premise not in self.scorer.customers.index:
            return None
        cust = self.scorer.customers.loc[premise]
        hx, hy = float(cust["x"]), float(cust["y"])
        home = self._lonlat([hx], [hy])[0].tolist()

        # P(damage) at this storm size for every asset in the scene (+ margin for home areas).
        near_idx = np.array(self.asset_tree.query_ball_point([hx, hy], RADIUS_M + HOME_AREA_M), dtype=int)
        p_all = np.zeros(len(self.assets))
        if near_idx.size:
            p_all[near_idx] = self.scorer.p(self.assets.iloc[near_idx], storm_n, area)
        scene = near_idx[np.hypot(self.assets["x"].to_numpy()[near_idx] - hx,
                                  self.assets["y"].to_numpy()[near_idx] - hy) <= RADIUS_M]
        sub = self.assets.iloc[scene]

        return {
            "home": home,
            "radius_m": RADIUS_M,
            "homes": self._homes(premise, hx, hy, p_all, home_outage_p),
            "poles": self._points(sub[sub["asset_type"] == "pole"], p_all),
            "equipment": self._points(sub[sub["asset_type"].isin(EQUIPMENT)], p_all, with_kind=True),
            "lines": self._lines(home, p_all),
            "supply_path": self._supply_path(premise, home, storm_n, area),
            "label": "MODEL_ESTIMATE",
        }

    def _homes(self, premise, hx, hy, p_all, home_outage_p) -> list[dict]:
        idx = self.scorer.customer_tree.query_ball_point([hx, hy], RADIUS_M)
        cust = self.scorer.customers.iloc[idx]
        xy = cust[["x", "y"]].to_numpy()
        # One house per building: several service points can share a location.
        _, keep = np.unique(np.round(xy, 0), axis=0, return_index=True)
        xy, ids = xy[keep], cust.index.to_numpy()[keep]
        neighbors = self.asset_tree.query_ball_point(xy, HOME_AREA_M)
        log_survive = np.log1p(-np.clip(p_all, 0, 0.999999))
        outage = np.array([1 - np.exp(log_survive[n].sum()) if n else 0.0 for n in neighbors])
        pos = self._lonlat(xy[:, 0], xy[:, 1])
        homes = [{"position": p.tolist(), "outage_p": round(float(o), 4), "is_you": False} for p, o in zip(pos, outage)]
        you = np.flatnonzero(ids == premise)
        if you.size:
            homes[you[0]]["is_you"] = True
            if home_outage_p is not None:  # the customer's own home uses the full premise model
                homes[you[0]]["outage_p"] = round(float(home_outage_p), 4)
        return homes

    def _points(self, df, p_all, with_kind=False) -> list[dict]:
        if df.empty:
            return []
        pos = self._lonlat(df["x"].to_numpy(), df["y"].to_numpy())
        rows = [self.row_by_uid[u] for u in df["asset_uid"]]
        out = []
        for p, r, kind in zip(pos, rows, df["asset_type"]):
            item = {"position": p.tolist(), "p": round(float(p_all[r]), 5)}
            if with_kind:
                item["kind"] = EQUIPMENT[kind]
            out.append(item)
        return out

    def _line_geometry(self, home) -> list[tuple]:
        """Lines near the home, cached: the bbox test (`&&`) uses the GIST index, then the exact distance."""
        key = tuple(home)
        if key in self._line_cache:
            return self._line_cache[key]
        sql = text("""
            WITH c AS (SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) AS pt)
            SELECT 'primary' AS kind, 'primary:' || id::bigint AS uid, usage, mounting,
                   ST_AsGeoJSON(ST_Simplify(geom, :tol)) AS geojson
            FROM primary_lines, c
            WHERE geom && ST_Expand(c.pt, :deg) AND ST_DWithin(geom::geography, c.pt::geography, :r)
            UNION ALL
            SELECT 'secondary', 'secondary:' || id::bigint, usage, mounting, ST_AsGeoJSON(ST_Simplify(geom, :tol))
            FROM secondary_lines, c
            WHERE geom && ST_Expand(c.pt, :deg2) AND ST_DWithin(geom::geography, c.pt::geography, :r2)""")
        with self.engine.connect() as conn:  # degrees margin covers the radius at this latitude
            rows = conn.execute(sql, {"lon": home[0], "lat": home[1], "r": RADIUS_M, "r2": SECONDARY_RADIUS_M,
                                      "deg": RADIUS_M / 80000, "deg2": SECONDARY_RADIUS_M / 80000,
                                      "tol": SIMPLIFY_DEG}).all()
        out = []
        for kind, uid, usage, mounting, geojson in rows:
            g = json.loads(geojson)
            parts = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
            overhead = mounting != "Underground"
            z = OVERHEAD_Z[kind] if overhead else 0.0
            paths = [[[round(c[0], 6), round(c[1], 6), z] for c in coords] for coords in parts]
            out.append((self.row_by_uid.get(uid), kind, overhead, usage == "Backbone", paths))
        self._line_cache[key] = out
        return out

    def _lines(self, home, p_all) -> list[dict]:
        out = []
        for r, kind, overhead, main_line, paths in self._line_geometry(home):
            p = round(float(p_all[r]), 5) if r is not None else 0.0
            out += [{"path": path, "p": p, "kind": kind, "overhead": overhead, "main_line": main_line} for path in paths]
        return out

    def _supply_path(self, premise, home, storm_n, area=None) -> list[dict]:
        """Home first, then each upline device in order (transformer -> fuses -> reclosers)."""
        if premise not in self._path_cache:
            with self.engine.connect() as conn:
                self._path_cache[premise] = conn.execute(text(
                    "SELECT subtype, location_n FROM premise_upline WHERE premise_nu = :p ORDER BY hop_seq"),
                    {"p": premise}).all()
        hops = self._path_cache[premise]
        nodes = [{"kind": "home", "label": "Your home", "position": home, "p": None}]
        devices = self.scorer.devices
        for subtype, loc in hops:
            if loc not in devices.index:  # e.g. the substation breaker isn't in the device extract
                continue
            d = devices.loc[loc]
            p = float(self.scorer.p(devices.loc[[loc]], storm_n, area)[0])  # devices can sit outside the scene radius
            name = DEVICE_NAMES.get(subtype, "a device on your line")
            nodes.append({"kind": subtype, "label": name[0].upper() + name[1:],
                          "position": self._lonlat([d["x"]], [d["y"]])[0].tolist(), "p": round(p, 4)})
        return nodes
