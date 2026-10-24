"""Entry point: create PostGIS, load every data/ file into Postgres, index, verify.

Idempotent: every table is dropped and recreated on each run.
    python -m db.load_all
"""
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from . import load_labor, load_shapefiles, load_topology
from .config import DATABASE_URL
from .sources import (
    FACILITIES,
    FACILITIES_TABLE,
    LABOR_FILE,
    SHAPEFILES,
    TOPOLOGY_EXPECTED_PREMISES,
    TOPOLOGY_FILE,
    TOPOLOGY_TABLE,
)

INDEX_COLUMNS = ("premise_nu", "location_n", "upstream_l", "aep_xfmr_l", "aep_locati", "circuit_key")

TABLE_COMMENTS = {
    **{s.table: f"{s.description} Source: data/{s.path}" for s in SHAPEFILES},
    "customer_private": (
        "BACKEND-ONLY sensitive customer flags (life support, threat). Never use as model "
        "features or send to the LLM. Join to customers on id. Source: data/Data1/Customer_Clip.shp"
    ),
    FACILITIES_TABLE: "Public hospitals, fire stations, ambulance services (reprojected from EPSG:3857).",
    TOPOLOGY_TABLE: f"Premise -> upline equipment path, hop_seq 1 = transformer. Source: data/{TOPOLOGY_FILE}",
    "labor_cu_hours": f"Install/remove hours per compatible unit (CNC sheet). Source: data/{LABOR_FILE}",
    "labor_cu_group_avg": "Average install/removal hours by CU group (and size where given).",
    "labor_adders": "Labor-only adders for access/site conditions (hours, per occurrence unless noted).",
    "labor_indirect_multiplier": "Indirect labor multiplier (travel, safety, setup) applied to labor hours.",
    "labor_cu_categories": "CU category codes and anonymized descriptions.",
}


def wait_for_db(engine: Engine, timeout_s: int = 60) -> None:
    deadline = time.time() + timeout_s
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(2)


def fill_customer_circuit_key(engine: Engine) -> None:
    """Customers only carry the station-local circuit_id, so take CIRnn from their transformer."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE customers ADD COLUMN IF NOT EXISTS circuit_key text"))
        conn.execute(text("""
            UPDATE customers c SET circuit_key = t.circuit_key
            FROM (SELECT DISTINCT ON (location_n) location_n, circuit_key
                  FROM transformers WHERE circuit_key IS NOT NULL
                  ORDER BY location_n) t
            WHERE t.location_n = c.aep_xfmr_l
        """))


def create_indexes(engine: Engine) -> None:
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = ANY(:cols)"
        ), {"cols": list(INDEX_COLUMNS)}).all()
        for table, col in rows:
            conn.execute(text(f'CREATE INDEX IF NOT EXISTS ix_{table}_{col} ON {table} ("{col}")'))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_customer_private_id ON customer_private (id)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{TOPOLOGY_TABLE}_location_n ON {TOPOLOGY_TABLE} (location_n)"))
        for table, comment in TABLE_COMMENTS.items():
            conn.execute(text(f"COMMENT ON TABLE {table} IS :c"), {"c": comment})


def verify(engine: Engine) -> list[str]:
    expected = {s.table: s.expected_rows for s in SHAPEFILES}
    expected["customer_private"] = expected["customers"]
    expected[FACILITIES_TABLE] = sum(n for _, n in FACILITIES.values())
    errors = []
    with engine.connect() as conn:
        for table, n in expected.items():
            got = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            if got != n:
                errors.append(f"{table}: expected {n:,} rows, got {got:,}")
        premises = conn.execute(text(f"SELECT count(DISTINCT premise_nu) FROM {TOPOLOGY_TABLE}")).scalar()
        if premises != TOPOLOGY_EXPECTED_PREMISES:
            errors.append(f"{TOPOLOGY_TABLE}: expected {TOPOLOGY_EXPECTED_PREMISES:,} premises, got {premises:,}")
        leaked = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'customers' AND column_name IN ('critical_c', 'special_op', 'esri_defau')"
        )).scalars().all()
        if leaked:
            errors.append(f"customers still has private columns: {leaked}")
        matched = conn.execute(text(
            "SELECT round(100.0 * avg((circuit_key IS NOT NULL)::int), 2) FROM customers"
        )).scalar()
        print(f"  customers with circuit_key: {matched}%")
    return errors


def main() -> int:
    engine = create_engine(DATABASE_URL)
    wait_for_db(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))

    print("Loading shapefiles...")
    load_shapefiles.load(engine)
    fill_customer_circuit_key(engine)
    print("Loading topology...")
    load_topology.load(engine)
    print("Loading labor workbook...")
    load_labor.load(engine)
    print("Indexing...")
    create_indexes(engine)
    print("Verifying...")
    errors = verify(engine)
    for e in errors:
        print(f"  ERROR {e}", file=sys.stderr)
    print("Done." if not errors else "Finished with errors.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
