"""Shapefile -> PostGIS tables. Geometry is stored as `geom` in EPSG:4326."""
import geopandas as gpd
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from .config import DATA_DIR
from .sources import FACILITIES, FACILITIES_TABLE, SHAPEFILES, ShapefileSource

TARGET_CRS = 4326


def read_shapefile(rel_path: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(DATA_DIR / rel_path, engine="pyogrio")
    gdf.columns = [c if c == gdf.geometry.name else c.lower() for c in gdf.columns]
    gdf = gdf.rename_geometry("geom")
    if gdf.crs is None or gdf.crs.to_epsg() != TARGET_CRS:
        gdf = gdf.to_crs(TARGET_CRS)
    # DBF pads/blanks text fields; store missing values as NULL.
    for col in gdf.select_dtypes(include="object").columns:
        if col != "geom":
            gdf[col] = gdf[col].map(lambda v: v.strip() or None if isinstance(v, str) else v)
    return gdf


def add_circuit_key(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Canonical circuit id = the anonymized `CIRnn` name.

    The 7-digit `circuit_id` is only unique within a station (e.g. 0000005 is both
    CIR5 and CIR6), so `aep_circui` / ARCOS `circuit` is used as the join key.
    Customers have no station field; their key is filled from their transformer
    in load_all.
    """
    if "aep_circui" in gdf.columns:
        gdf["circuit_key"] = gdf["aep_circui"]
    elif "circuit" in gdf.columns:
        gdf["circuit_key"] = gdf["circuit"]
    return gdf


def dates_to_date_type(engine: Engine, table: str) -> None:
    """DBF date fields have no time component; store them as DATE, not TIMESTAMP."""
    with engine.begin() as conn:
        cols = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t AND data_type LIKE 'timestamp%'"
            ),
            {"t": table},
        ).scalars().all()
        for col in cols:
            conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN "{col}" TYPE date USING "{col}"::date'))


def write(engine: Engine, gdf: gpd.GeoDataFrame, table: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    gdf.to_postgis(table, engine, if_exists="replace", index=False, chunksize=10_000)
    dates_to_date_type(engine, table)


def load_source(engine: Engine, src: ShapefileSource) -> None:
    gdf = add_circuit_key(read_shapefile(src.path))
    if src.private_cols:
        keys = ["id", "premise_nu"]
        private = pd.DataFrame(gdf[keys + list(src.private_cols)])
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {src.private_table} CASCADE"))
        private.to_sql(src.private_table, engine, index=False, chunksize=10_000)
        gdf = gdf.drop(columns=list(src.private_cols))
    write(engine, gdf, src.table)
    print(f"  {src.table}: {len(gdf):,} rows")


def load_facilities(engine: Engine) -> None:
    frames = []
    for facility_type, (path, _) in FACILITIES.items():
        gdf = read_shapefile(path)
        gdf.insert(0, "facility_type", facility_type)
        frames.append(gdf)
    gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geom", crs=TARGET_CRS)
    write(engine, gdf, FACILITIES_TABLE)
    print(f"  {FACILITIES_TABLE}: {len(gdf):,} rows")


def load(engine: Engine) -> None:
    for src in SHAPEFILES:
        load_source(engine, src)
    load_facilities(engine)
