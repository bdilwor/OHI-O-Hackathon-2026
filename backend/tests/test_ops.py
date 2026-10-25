"""AEP Operations dashboard: area-wide KPIs, priority facilities, privacy and read-only behavior."""
import json
import re

from sqlalchemy import text


def overview(client, **params):
    r = client.get("/ops/overview", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_kpis_rise_with_storm_intensity(client):
    small, big = overview(client, mode="sandbox", intensity=1), overview(client, mode="sandbox", intensity=5)
    for k in ("expected_damaged_assets", "expected_customers_out", "crew_hours", "crews_for_24h", "crews_for_72h"):
        assert big["kpis"][k] > small["kpis"][k], k
    assert big["kpis"]["crews_for_24h"] >= big["kpis"]["crews_for_72h"]
    assert "service area" in big["storm"]["summary"]


def test_circuits_ranked_and_facilities_listed(client, engine):
    d = overview(client, mode="sandbox", intensity=4)
    damage = [c["expected_damage"] for c in d["circuits"]]
    assert damage == sorted(damage, reverse=True) and len(d["circuits"]) == 10
    assert all(c["circuit"] and c["position"] for c in d["circuits"])
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM priority_facilities")).scalar()
    assert len(d["facilities"]) == n
    assert {f["type"] for f in d["facilities"]} == {"hospital", "fire_station", "ambulance"}
    assert all(f["action"] and f["risk_band"] in {"Low", "Medium", "High", "Very High"} for f in d["facilities"])


def test_crews_input_gives_time_to_restore(client):
    d = overview(client, mode="sandbox", intensity=3, crews=50)
    assert d["kpis"]["hours_to_restore_all"] == round(d["kpis"]["crew_hours"] / 50, 1)


def test_forecast_mode_and_validation(client):
    d = overview(client, mode="forecast", day=1)  # fixture: calm day
    assert d["storm"]["label"] == "Calm" and d["kpis"]["expected_customers_out"] < 50
    assert client.get("/ops/overview", params={"mode": "sandbox", "intensity": 9}).status_code == 422
    assert client.get("/ops/overview", params={"mode": "replay"}).status_code == 422


def test_no_customer_level_data(client):
    blob = json.dumps(overview(client, mode="sandbox", intensity=5))
    assert not re.search(r"\b\d{9}\b", blob)  # premise numbers
    for field in ("premise", "address", "critical_c", "esri_defau", "life_support", "anno_strin", "road_name"):
        assert field not in blob, field


def test_ops_writes_nothing(client, engine):
    def counts():
        with engine.connect() as conn:
            return [conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                    for t in ("customers", "asset_risk", "priority_facilities")]
    before = counts()
    overview(client, mode="sandbox", intensity=5)
    assert counts() == before
