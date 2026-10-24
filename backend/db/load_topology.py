"""matched_upstream.txt -> premise_upline (one row per premise per upline hop).

Each line: premise<TAB>hop<TAB>hop...  where hop = type*id*subtype*location,
ordered transformer -> fuses/reclosers -> circuit breaker (hop_seq 1 = nearest).
"""
import io

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .config import DATA_DIR
from .sources import TOPOLOGY_FILE, TOPOLOGY_TABLE

DDL = f"""
DROP TABLE IF EXISTS {TOPOLOGY_TABLE} CASCADE;
CREATE TABLE {TOPOLOGY_TABLE} (
    premise_nu  text    NOT NULL,
    hop_seq     integer NOT NULL,
    equip_class text    NOT NULL,  -- eo_power_xfrmr_inst / eo_isolating_eqpt_inst / eo_circuit
    equip_id    bigint  NOT NULL,
    subtype     text    NOT NULL,  -- Distribution, Fuse, Recloser (Hydraulic), Circuit Breaker, ...
    location_n  text    NOT NULL,  -- matches <device table>.location_n (circuit_id for breakers)
    PRIMARY KEY (premise_nu, hop_seq)
);
"""


def parse(path) -> io.StringIO:
    buf = io.StringIO()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            fields = line.rstrip("\r\n").split("\t")
            premise, hops = fields[0], [h for h in fields[1:] if h]
            for seq, hop in enumerate(hops, start=1):
                equip_class, equip_id, subtype, location = hop.split("*")
                buf.write(f"{premise}\t{seq}\t{equip_class}\t{equip_id}\t{subtype}\t{location}\n")
    buf.seek(0)
    return buf


def load(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(DDL))
    buf = parse(DATA_DIR / TOPOLOGY_FILE)
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.copy_expert(f"COPY {TOPOLOGY_TABLE} FROM STDIN", buf)
        raw.commit()
    finally:
        raw.close()
    with engine.connect() as conn:
        n = conn.execute(text(f"SELECT count(*) FROM {TOPOLOGY_TABLE}")).scalar()
    print(f"  {TOPOLOGY_TABLE}: {n:,} rows")
