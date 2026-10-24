"""2D/3D map payload: the model's prediction per forecast day or replay; shape, linkage, privacy."""
import json
import re

from .constants import DEMO_PREMISE


def get(client, premise=DEMO_PREMISE, **params):
    return client.get("/map/context", params={"premise": premise, **params})


def test_map_payload_shape(client):
    m = get(client, day=0).json()
    assert len(m["home"]) == 2 and -81 < m["home"][0] < -79 and 36.5 < m["home"][1] < 38
    assert m["homes"] and m["poles"] and m["equipment"] and m["lines"]
    assert sum(h["is_you"] for h in m["homes"]) == 1
    assert {e["kind"] for e in m["equipment"]} <= {"Transformer", "Fuse", "Recloser", "Switch"}
    assert all(len(pt) == 3 for line in m["lines"] for pt in line["path"])  # 3D wires
    assert any(line["overhead"] and line["path"][0][2] > 0 for line in m["lines"])
    assert m["forecast"]["available"] and len(m["forecast"]["days"]) == 7


def test_your_home_uses_the_premise_model(client):
    m = get(client, day=0).json()
    you = next(h for h in m["homes"] if h["is_you"])
    assert you["outage_p"] == m["your_risk"]["probability"]


def test_calm_day_is_greener_than_storm_and_replay(client):
    def mean_home(**p):
        homes = get(client, **p).json()["homes"]
        return sum(h["outage_p"] for h in homes) / len(homes)
    calm, storm, replay = mean_home(day=1), mean_home(day=3), mean_home(mode="replay", storm="feb2025")
    assert calm < storm and calm < replay


def test_storm_context_reported(client):
    m = get(client, mode="replay", storm="feb2025").json()
    assert m["storm"]["hypothetical"] and m["storm"]["mode"] == "replay"
    assert get(client, day=1).json()["storm"]["label"] == "Calm"


def test_supply_path_is_ordered_from_home(client):
    path = get(client, day=0).json()["supply_path"]
    assert path[0]["kind"] == "home" and path[1]["kind"] == "Distribution"
    assert len(path) >= 3 and all(n["label"] for n in path)


def test_map_payload_has_no_ids(client):
    blob = json.dumps(get(client, day=0).json())
    for pattern in [r"CIR\d+", r"location_n|asset_uid|premise", r"\b\d{8}[A-Z0-9]\d{5}\b", DEMO_PREMISE]:
        assert not re.search(pattern, blob), pattern


def test_unknown_premise_404(client):
    r = get(client, premise="999")
    assert r.status_code == 404 and "couldn't find" in r.json()["detail"]


def test_bad_params_rejected(client):
    assert get(client, day=9).status_code == 422
    assert get(client, mode="replay", storm="atlantis").status_code == 422
