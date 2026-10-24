"""Asset table, storm clustering, label snapping and leak-free feature rows.

Shared by build_dataset (training rows), train (full held-out storms) and
score_assets (current risk for every asset).
"""
import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sqlalchemy.engine import Engine

from .db import read_geo

STORM_GAP_DAYS = 3  # report dates within 3 days belong to the same storm
MAJOR_STORM_MIN_RECORDS = 40  # major storms get their own CV fold
SNAP_DISTANCE_M = 30
SNAP_FALLBACK_M = 60
SNAP_MIN_MATCH_RATE = 0.8
POLE_CIRCUIT_SNAP_M = 50
WIRE_SPEC_TOP_N = 15

ARCOS_TO_ASSET = {
    "Pole": "pole",
    "primary": "primary",
    "secondary": "secondary",
    "Fuse Bank": "fuse",
    "Transformer": "transformer",
    "Recloser Bank": "recloser",
    "Switch": "switch",
}

NUMERIC = [
    "age_years", "height_ft", "pole_class_num", "voltage_kv", "segment_len_m", "kva",
    "fuse_size_a", "overhead", "bare_conductor", "customers_downstream",
    "n_upline_devices", "n_upline_reclosers",
    "hist_asset_damaged", "hist_circuit_share", "hist_share_250m", "hist_share_500m",
    "hist_access_rate_500m", "storm_log_damage",
]
CATEGORICAL = ["asset_type", "material", "line_usage", "wire_spec_top", "device_subtype", "owner_type"]
HISTORY = [f for f in NUMERIC if f.startswith("hist_")]

# Only engineering attributes are selected; no customer table is ever read here.
ASSET_SQL = {
    "pole": """SELECT id, location_n, NULL AS circuit_key, date_insta AS install_date, material,
                      height, "class" AS pole_class, usage AS device_subtype, owner_type, geom FROM poles""",
    "primary": """SELECT id, location_n, circuit_key, aep_date_i AS install_date, usage AS line_usage,
                         mounting, nominal_vo, a_phase_sp AS wire_spec, owner_type, geom FROM primary_lines""",
    "secondary": """SELECT id, location_n, circuit_key, aep_date_i AS install_date, usage AS line_usage,
                           mounting, nominal_vo, a_phase_sp AS wire_spec, owner_type, geom FROM secondary_lines""",
    "fuse": """SELECT id, location_n, circuit_key, a_phase_da AS install_date, mounting, nominal_vo,
                      a_phase_sp AS device_spec, aep_custom, upstream_l, device_typ AS device_subtype,
                      owner_type, geom FROM fuse_banks""",
    "transformer": """SELECT id, location_n, circuit_key, a_phase_da AS install_date, mounting,
                             a_phase_sp AS device_spec, aep_custom, upstream_l,
                             structure_ AS device_subtype, owner_type, geom FROM transformers""",
    "recloser": """SELECT id, location_n, circuit_key, a_phase_da AS install_date, nominal_vo,
                          aep_custom, upstream_l, device_typ AS device_subtype, owner_type, geom
                   FROM recloser_banks""",
    "switch": """SELECT id, location_n, circuit_key, a_phase_da AS install_date, mounting, nominal_vo,
                        aep_custom, upstream_l, device_typ AS device_subtype, owner_type, geom
                 FROM switches""",
}
LINE_TYPES = ("primary", "secondary")


def _first_number(s: pd.Series, pattern: str) -> pd.Series:
    return pd.to_numeric(s.astype("string").str.extract(pattern, expand=False), errors="coerce")


def _upline_counts(assets: pd.DataFrame) -> pd.DataFrame:
    """Walk the device `upstream_l` chain: how many devices / reclosers sit above each device."""
    devices = assets.dropna(subset=["location_n"])
    devices = devices[devices["asset_type"].isin(["fuse", "transformer", "recloser", "switch"])]
    upstream = dict(zip(devices["location_n"], devices["upstream_l"]))
    reclosers = set(devices.loc[devices["asset_type"] == "recloser", "location_n"])
    n_dev, n_rec = {}, {}
    for loc in upstream:
        seen, cur, nd, nr = {loc}, upstream.get(loc), 0, 0
        while cur and cur not in seen and len(seen) < 100:
            seen.add(cur)
            nd += 1
            nr += cur in reclosers
            cur = upstream.get(cur)
        n_dev[loc], n_rec[loc] = nd, nr
    is_device = assets["asset_type"].isin(["fuse", "transformer", "recloser", "switch"])
    return pd.DataFrame({
        "n_upline_devices": assets["location_n"].map(n_dev).where(is_device),
        "n_upline_reclosers": assets["location_n"].map(n_rec).where(is_device),
    })


def load_assets(engine: Engine) -> gpd.GeoDataFrame:
    frames = []
    for asset_type, sql in ASSET_SQL.items():
        gdf = read_geo(sql, engine)
        gdf.insert(0, "asset_type", asset_type)
        frames.append(gdf)
    a = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geom", crs=frames[0].crs)
    a["asset_uid"] = a["asset_type"] + ":" + a["id"].astype("int64").astype(str)
    assert a["asset_uid"].is_unique, "asset ids are not unique within a table"

    is_line = a["asset_type"].isin(LINE_TYPES)
    rep = a.geometry.representative_point()
    rep[is_line] = a.loc[is_line].geometry.interpolate(0.5, normalized=True)
    a["x"], a["y"] = rep.x.to_numpy(), rep.y.to_numpy()

    a["install_date"] = pd.to_datetime(a["install_date"], errors="coerce")
    a["segment_len_m"] = a.geometry.length.where(is_line)
    a["overhead"] = a["mounting"].map({"Overhead": 1.0, "Underground": 0.0})
    a.loc[a["asset_type"] == "pole", "overhead"] = 1.0
    a["height_ft"] = _first_number(a["height"], r"(\d+)")
    a["pole_class_num"] = _first_number(a["pole_class"], r"^(\d+)$")
    a["voltage_kv"] = _first_number(a["nominal_vo"], r"([\d.]+)")
    spec = a["wire_spec"].astype("string")
    a["bare_conductor"] = np.where(spec.isna(), np.nan, spec.fillna("").str.contains("BARE"))
    top_specs = spec[is_line].value_counts().head(WIRE_SPEC_TOP_N).index
    a["wire_spec_top"] = np.where(spec.isin(top_specs), spec, np.where(spec.notna(), "other", None))
    a["kva"] = _first_number(a["device_spec"].where(a["asset_type"] == "transformer"), r"([\d.]+)\s*kVA")
    a["fuse_size_a"] = _first_number(a["device_spec"].where(a["asset_type"] == "fuse"), r"FUSE\s+([\d.]+)")
    a["customers_downstream"] = pd.to_numeric(a["aep_custom"], errors="coerce")
    a = a.join(_upline_counts(a))

    # Poles carry no circuit; inherit it from the nearest primary segment.
    poles = a[a["asset_type"] == "pole"][["geom"]]
    prim = a[a["asset_type"] == "primary"][["circuit_key", "geom"]]
    near = gpd.sjoin_nearest(poles, prim, max_distance=POLE_CIRCUIT_SNAP_M)
    near = near[~near.index.duplicated()]
    a.loc[near.index, "circuit_key"] = near["circuit_key"]

    for col in CATEGORICAL:
        if col != "asset_type":
            a[col] = a[col].fillna("unknown").astype(str)
    for col in NUMERIC:  # nullable pandas dtypes (Float64/<NA>) break sklearn
        if col in a.columns:
            a[col] = pd.to_numeric(a[col], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
    keep = ["asset_uid", "asset_type", "location_n", "circuit_key", "install_date", "x", "y", "geom",
            *[c for c in NUMERIC + CATEGORICAL if c in a.columns]]
    return a[list(dict.fromkeys(keep))]


def load_arcos(engine: Engine) -> gpd.GeoDataFrame:
    arcos = read_geo(
        """SELECT row_number() OVER () AS arcos_row, circuit_key, report_tim, repaired, repaired_d,
                  asset_type AS arcos_asset_type, inform_wor, access_iss, special_eq, geom
           FROM arcos_damage""",
        engine,
    )
    arcos["report_tim"] = pd.to_datetime(arcos["report_tim"])
    arcos["repaired_d"] = pd.to_datetime(arcos["repaired_d"])
    arcos["x"], arcos["y"] = arcos.geometry.x, arcos.geometry.y
    arcos["access_yes"] = (arcos["access_iss"] == "Yes").astype(int)
    return arcos


def cluster_storms(arcos: pd.DataFrame) -> pd.DataFrame:
    """Assign storm_id by chaining report dates <= STORM_GAP_DAYS apart. Returns the storms table."""
    dates = np.sort(arcos["report_tim"].unique())
    gaps = np.diff(dates).astype("timedelta64[D]").astype(int) > STORM_GAP_DAYS
    ids = np.concatenate([[0], np.cumsum(gaps)])
    arcos["storm_id"] = arcos["report_tim"].map(dict(zip(dates, ids)))
    storms = arcos.groupby("storm_id").agg(
        storm_start=("report_tim", "min"), storm_end=("report_tim", "max"), n_records=("report_tim", "size")
    ).reset_index()
    major = storms["n_records"] >= MAJOR_STORM_MIN_RECORDS
    storms["cv_group"] = np.where(major, "storm_" + storms["storm_start"].dt.strftime("%Y-%m"), "minor_events")
    storms["storm_log_damage"] = np.log1p(storms["n_records"])
    return storms


def severity_levels(storms: pd.DataFrame) -> dict:
    """Scenario severities anchored to real past events (damage-record counts)."""
    by_size = storms.sort_values("n_records", ascending=False)
    minor = storms[(storms["n_records"] < MAJOR_STORM_MIN_RECORDS) & (storms["n_records"] >= 10)]
    hi, med = by_size.iloc[0], by_size.iloc[1]
    return {
        "low": {"n_records": float(minor["n_records"].median()),
                "reference": f"a typical minor event ({len(minor)} past events with 10-39 damage reports)"},
        "medium": {"n_records": float(med["n_records"]),
                   "reference": f"the {med['storm_start']:%B %Y} storm ({med['n_records']} damage reports)"},
        "high": {"n_records": float(hi["n_records"]),
                 "reference": f"the {hi['storm_start']:%B %Y} storm ({hi['n_records']} damage reports)"},
    }


def snap_labels(arcos: gpd.GeoDataFrame, assets: gpd.GeoDataFrame) -> tuple[pd.Series, pd.DataFrame]:
    """Match each ARCOS record to the nearest asset of its type (same circuit first)."""
    matched = pd.Series(pd.NA, index=arcos.index, dtype="object")
    log = []
    for arcos_type, asset_type in ARCOS_TO_ASSET.items():
        pts = arcos[arcos["arcos_asset_type"] == arcos_type]
        cand = assets.loc[assets["asset_type"] == asset_type, ["asset_uid", "circuit_key", "geom"]]
        rates = {}
        for max_d in (SNAP_DISTANCE_M, SNAP_FALLBACK_M):
            todo = pts[matched.loc[pts.index].isna()]
            same = 0
            for ck, grp in todo.groupby("circuit_key"):
                c = cand[cand["circuit_key"] == ck]
                if len(c):
                    j = gpd.sjoin_nearest(grp[["geom"]], c[["asset_uid", "geom"]], max_distance=max_d)
                    j = j[~j.index.duplicated()]
                    matched.loc[j.index] = j["asset_uid"]
                    same += len(j)
            todo = pts[matched.loc[pts.index].isna()]
            if len(todo):
                j = gpd.sjoin_nearest(todo[["geom"]], cand[["asset_uid", "geom"]], max_distance=max_d)
                j = j[~j.index.duplicated()]
                matched.loc[j.index] = j["asset_uid"]
            rates[max_d] = matched.loc[pts.index].notna().mean() if len(pts) else 1.0
            if rates[max_d] >= SNAP_MIN_MATCH_RATE:
                break
        log.append({
            "arcos_asset_type": arcos_type, "records": len(pts),
            "matched": int(matched.loc[pts.index].notna().sum()),
            "match_rate": round(float(rates[max(rates)]), 4), "max_distance_m": max(rates),
        })
    skipped = arcos[~arcos["arcos_asset_type"].isin(ARCOS_TO_ASSET)]
    log.append({"arcos_asset_type": "(not modeled: " + ", ".join(sorted(skipped["arcos_asset_type"].fillna("blank").unique())) + ")",
                "records": len(skipped), "matched": 0, "match_rate": 0.0, "max_distance_m": None})
    return matched, pd.DataFrame(log)


def history_features(assets: pd.DataFrame, prior: pd.DataFrame) -> pd.DataFrame:
    """Damage history near each asset, from `prior` ARCOS records only (caller guarantees no future storms).

    Spatial/circuit history is expressed as a *share* of all prior damage so it stays comparable
    over time (raw counts only grow, which made later storms look different from earlier ones).
    """
    out = pd.DataFrame(index=assets.index)
    n_prior = len(prior)
    if n_prior == 0:
        for f in HISTORY:
            out[f] = 0.0
        out["hist_access_rate_500m"] = np.nan
        return out
    pts = assets[["x", "y"]].to_numpy()
    tree = cKDTree(prior[["x", "y"]].to_numpy())
    n250 = tree.query_ball_point(pts, 250, return_length=True)
    n500 = tree.query_ball_point(pts, 500, return_length=True)
    out["hist_share_250m"] = n250 / n_prior
    out["hist_share_500m"] = n500 / n_prior
    yes = prior[prior["access_yes"] == 1]
    n_yes = (cKDTree(yes[["x", "y"]].to_numpy()).query_ball_point(pts, 500, return_length=True)
             if len(yes) else np.zeros(len(pts)))
    out["hist_access_rate_500m"] = np.where(n500 > 0, n_yes / np.clip(n500, 1, None), np.nan)
    circuit_counts = assets["circuit_key"].map(prior["circuit_key"].value_counts()).fillna(0).to_numpy()
    out["hist_circuit_share"] = circuit_counts / n_prior
    out["hist_asset_damaged"] = assets["asset_uid"].map(prior["matched_uid"].value_counts()).fillna(0).to_numpy()
    return out


def feature_rows(assets: pd.DataFrame, prior: pd.DataFrame, as_of: pd.Timestamp, storm_log_damage: float) -> pd.DataFrame:
    """Model-ready features for `assets` given prior damage, an as-of date and a storm severity."""
    rows = assets.drop(columns=["geom"], errors="ignore").copy()
    rows["age_years"] = (as_of - rows["install_date"]).dt.days / 365.25
    rows["storm_log_damage"] = storm_log_damage
    return rows.join(history_features(rows, prior))


def make_rows(assets: pd.DataFrame, arcos: pd.DataFrame, storms: pd.DataFrame, storm_ids,
              neg_rate: float, seed: int = 0) -> pd.DataFrame:
    """(asset, storm) rows for the given storms: all positives + `neg_rate` of negatives, weighted back."""
    rng = np.random.default_rng(seed)
    parts = []
    for sid in storm_ids:
        storm = storms.set_index("storm_id").loc[sid]
        prior = arcos[arcos["storm_id"] < sid]
        assert (prior["report_tim"] < storm["storm_start"]).all(), "history leak: prior record not before storm"
        pos_uids = set(arcos.loc[(arcos["storm_id"] == sid), "matched_uid"].dropna())
        is_pos = assets["asset_uid"].isin(pos_uids).to_numpy()
        keep = is_pos | (rng.random(len(assets)) < neg_rate)
        sub = assets.loc[keep]
        rows = feature_rows(sub, prior, storm["storm_start"], storm["storm_log_damage"])
        rows["label"] = is_pos[keep].astype(int)
        rows["sample_weight"] = np.where(rows["label"] == 1, 1.0, 1.0 / neg_rate)
        rows["storm_id"] = sid
        rows["cv_group"] = storm["cv_group"]
        parts.append(rows)
    return pd.concat(parts, ignore_index=True)
