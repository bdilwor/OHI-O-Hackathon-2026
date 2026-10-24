"""Is the raw data loaded into Postgres?"""
import pytest
from sqlalchemy import inspect, text

from db.sources import SHAPEFILES, TOPOLOGY_EXPECTED_PREMISES, TOPOLOGY_TABLE

PRIVATE_COLS = {"critical_c", "esri_defau", "special_op"}


@pytest.mark.parametrize("src", SHAPEFILES, ids=lambda s: s.table)
def test_table_row_counts(engine, src):
    with engine.connect() as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {src.table}")).scalar() == src.expected_rows


def test_topology_covers_all_premises(engine):
    with engine.connect() as conn:
        n = conn.execute(text(f"SELECT count(DISTINCT premise_nu) FROM {TOPOLOGY_TABLE}")).scalar()
    assert n == TOPOLOGY_EXPECTED_PREMISES


def test_private_fields_split_out(engine):
    cols = {c["name"] for c in inspect(engine).get_columns("customers")}
    assert not cols & PRIVATE_COLS
    assert PRIVATE_COLS <= {c["name"] for c in inspect(engine).get_columns("customer_private")}
