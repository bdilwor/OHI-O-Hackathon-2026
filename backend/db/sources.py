"""Declarative mapping of every source file in data/ to its Postgres table."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ShapefileSource:
    path: str  # relative to DATA_DIR
    table: str
    expected_rows: int
    description: str
    # Columns moved out of the main table into `<private_table>` (never LLM-facing).
    private_cols: tuple[str, ...] = ()
    private_table: str | None = None
    extra: dict = field(default_factory=dict)


SHAPEFILES = [
    ShapefileSource(
        "Data1/arcos_prod_damage_09_28_Clip.shp", "arcos_damage", 1_659,
        "ARCOS storm damage assessments (day-level report/repair dates).",
    ),
    ShapefileSource(
        "Data1/Customer_Clip.shp", "customers", 117_642,
        "Customer premises / service points. Sensitive flags live in customer_private.",
        private_cols=("critical_c", "special_op", "esri_defau"),
        private_table="customer_private",
    ),
    ShapefileSource("Data1/Pole_Clip.shp", "poles", 107_013, "Distribution poles."),
    ShapefileSource(
        "Data1/Primary_ExportFeatures.shp", "primary_lines", 62_788,
        "Primary conductor segments (Backbone/Lateral).",
    ),
    ShapefileSource(
        "Data2/Secondary_ExportFeatures.shp", "secondary_lines", 127_403,
        "Secondary / service conductor segments.",
    ),
    ShapefileSource("Data1/Fuse_Bank_Clip.shp", "fuse_banks", 7_616, "Fuse banks."),
    ShapefileSource("Data2/Recloser_Bank_Clip.shp", "recloser_banks", 331, "Recloser banks."),
    ShapefileSource("Data2/Switch_Clip.shp", "switches", 900, "Switches."),
    ShapefileSource("Data2/Transformer_Clip.shp", "transformers", 26_959, "Distribution transformers."),
]

# Public priority-location layers (EPSG:3857) unioned into one table.
FACILITIES_TABLE = "priority_facilities"
FACILITIES = {
    "hospital": ("Data1/Hospitals_Medic_Pairwis_Clip.shp", 5),
    "fire_station": ("Data1/Fire_Stations_E_Pairwis_Clip.shp", 37),
    "ambulance": ("Data1/Ambulance_Servi_Pairwis_Clip.shp", 10),
}

TOPOLOGY_FILE = "matched_upstream.txt"
TOPOLOGY_TABLE = "premise_upline"
TOPOLOGY_EXPECTED_PREMISES = 117_599

LABOR_FILE = "Install_Labor_HackOHIO.xlsx"
