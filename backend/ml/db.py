import io
from pathlib import Path

import geopandas as gpd
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from db.config import DATABASE_URL

METRIC_CRS = 32617  # UTM 17N, meters
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"


def get_engine() -> Engine:
    return create_engine(DATABASE_URL)


def read_geo(sql: str, engine: Engine) -> gpd.GeoDataFrame:
    """Read a PostGIS query (geometry column `geom`) and project to meters."""
    return gpd.read_postgis(sql, engine, geom_col="geom").to_crs(METRIC_CRS)


def read_frame(sql: str, engine: Engine, **read_csv_kwargs) -> pd.DataFrame:
    """Bulk-read a query via COPY ... TO STDOUT (several times faster than pandas.read_sql for big tables)."""
    buf = io.StringIO()
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER)", buf)
    finally:
        raw.close()
    buf.seek(0)
    return pd.read_csv(buf, **read_csv_kwargs)
