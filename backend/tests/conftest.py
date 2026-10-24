"""Shared fixtures. Run inside Docker:

    docker compose up -d db
    docker compose run --rm ml pytest -q tests
"""
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from ml.db import get_engine

SAMPLE_PREMISES = 200


@pytest.fixture(scope="session")
def engine():
    eng = get_engine()
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as e:
        pytest.skip(f"database unreachable ({e.__class__.__name__}); start it with `docker compose up -d db`")
    return eng


@pytest.fixture(scope="session")
def scored_db(engine):
    """Engine, but only once the ML pipeline has published asset_risk."""
    if not inspect(engine).has_table("asset_risk"):
        pytest.skip("asset_risk table missing: run the ML pipeline first with `docker compose run --rm ml`")
    return engine


@pytest.fixture(scope="session")
def bundle():
    from ml.model import BUNDLE_PATH, load_bundle

    if not BUNDLE_PATH.exists():
        pytest.skip("model.joblib missing: run `docker compose run --rm ml`")
    return load_bundle()


@pytest.fixture(scope="session")
def scorer(scored_db, bundle):
    from ml.score import PremiseScorer

    return PremiseScorer(scored_db)


@pytest.fixture(scope="session")
def sample_premises(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT premise_nu FROM customers WHERE premise_nu IS NOT NULL "
            "ORDER BY md5(premise_nu) LIMIT :n"), {"n": SAMPLE_PREMISES}).scalars().all()



@pytest.fixture(scope="session")
def client(scored_db, tmp_path_factory):
    """FastAPI app in-process, offline LLM mode, audit log in a temp dir."""
    import os

    os.environ["LLM_PROVIDER"] = "offline"
    os.environ["AUDIT_LOG_PATH"] = str(tmp_path_factory.mktemp("audit") / "audit.jsonl")
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def fresh_storm(client):
    client.post("/storm/reset")
    yield
    client.post("/storm/reset")
