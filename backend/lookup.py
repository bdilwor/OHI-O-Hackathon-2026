"""Resolve a customer's own service location from an address or map point.

Returns at most 3 candidates for an "Is this your home?" confirmation, each with a masked
address. Never returns another customer's details or any private flag.
"""
import re

import numpy as np
from pyproj import Transformer
from sqlalchemy import text
from sqlalchemy.engine import Engine

from ml.db import METRIC_CRS

MAX_CANDIDATES = 3
POINT_SNAP_M = 30  # ~87% of points within 30 m snap to the right service point; service points ~45 m apart
STREET_SUFFIXES = {
    "RD", "ROAD", "ST", "STREET", "DR", "DRIVE", "LN", "LANE", "AVE", "AV", "AVENUE", "CT", "COURT", "CIR",
    "BLVD", "BOULEVARD", "WAY", "PL", "PLACE", "TRL", "TRAIL", "PKWY", "PARKWAY", "HWY", "HIGHWAY", "TER",
    "TERRACE", "SW", "SE", "NW", "NE", "N", "S", "E", "W",
}
SERVICE = {"Overhead": "overhead", "Underground": "underground"}


def mask_house_number(num: str | None) -> str:
    num = (num or "").strip()
    return num[0] + "*" * (len(num) - 1) if num else "***"


def parse_address(q: str) -> tuple[str, str, str | None]:
    """'7110 Rabbit Run Rd, Roanoke VA' -> ('7110', 'RABBIT RUN', 'ROANOKE')."""
    q = re.sub(r"[^\w\s,-]", " ", q).upper().strip()
    street_part, _, city_part = q.partition(",")
    m = re.match(r"^\s*(\d+[A-Z]?)\s+(.+)$", street_part)
    if not m:
        raise ValueError("Please include your house number and street, e.g. '7110 Rabbit Run, Roanoke'.")
    words = m.group(2).split()
    while len(words) > 1 and words[-1] in STREET_SUFFIXES:
        words.pop()
    city = re.sub(r"\b(VA|VIRGINIA|\d{5})\b", "", city_part).strip() or None
    return m.group(1), " ".join(words), city


class Lookup:
    def __init__(self, engine: Engine, scorer):
        self.engine = engine
        self.scorer = scorer
        self.to_metric = Transformer.from_crs(4326, METRIC_CRS, always_xy=True)

    def _candidates(self, where: str, params: dict) -> list[dict]:
        sql = f"""SELECT DISTINCT ON (premise_nu) premise_nu, anno_strin, road_name, city, kind_of_se, circuit_key
                  FROM customers WHERE premise_nu IS NOT NULL AND {where}
                  ORDER BY premise_nu LIMIT 50"""
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text(sql), params)]

    @staticmethod
    def _card(row: dict) -> dict:
        return {
            "premise": row["premise_nu"],  # opaque token for this customer's own session
            "masked_address": f"{mask_house_number(row['anno_strin'])} {row['road_name']}, {row['city']}".strip(),
            "service_type": SERVICE.get(row["kind_of_se"], "unknown"),
            "circuit": row["circuit_key"],
        }

    def by_address(self, q: str) -> dict:
        num, street, city = parse_address(q)
        params = {"num": num, "street": street, "city": city}
        city_sql = " AND city = :city" if city else ""
        rows = self._candidates(f"anno_strin = :num AND road_name = :street{city_sql}", params)
        if not rows:  # tolerate partial street names and unit suffixes on the house number
            params["street_like"] = street.split()[0] + "%"
            params["num_like"] = num + "%"
            rows = self._candidates(f"anno_strin ILIKE :num_like AND road_name ILIKE :street_like{city_sql}", params)
        return self._result(rows, "address")

    def by_point(self, lat: float, lon: float) -> dict:
        x, y = self.to_metric.transform(lon, lat)
        if not (np.isfinite(x) and np.isfinite(y)):
            return self._result([], "map point")
        dist, idx = self.scorer.customer_tree.query([x, y])
        if dist > POINT_SNAP_M:
            return self._result([], "map point")
        premise = self.scorer.customer_ids[idx]
        return self._result(self._candidates("premise_nu = :p", {"p": premise}), "map point")

    def _result(self, rows: list[dict], how: str) -> dict:
        if not rows:
            return {"found": False, "candidates": [],
                    "message": f"We couldn't match that {how} to a service location. Try your full street "
                               "address, or tap closer to your home on the map."}
        out = {"found": True, "candidates": [self._card(r) for r in rows[:MAX_CANDIDATES]],
               "question": "Is this your home?"}
        if len(rows) > MAX_CANDIDATES:
            out["more_matches"] = True
            out["message"] = "Several service points match; add your city or unit to narrow it down."
        return out
