from pathlib import Path

import geopandas as gpd
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
