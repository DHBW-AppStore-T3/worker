"""Pytest configuration and fixtures."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# ``app.config`` requires DATABASE_URL. Unit tests never connect; the
# integration tests use this database (CI: the Postgres service) and build
# the worker's part of the schema in it (tests/schema.sql).
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/worker_test")

SCHEMA_SQL = Path(__file__).with_name("schema.sql")


# ----------------------------------------------------------------
# Auto-marker policy
# ----------------------------------------------------------------
# CI rennt zwei getrennte Lanes: ``pytest -m unit`` und
# ``pytest -m integration``. Tests, die *gar keinen* Marker tragen,
# fallen aus **beiden** Selektoren raus — d.h. sie würden lautlos
# nicht in CI ausgeführt, obwohl sie lokal grün sind. Genau das ist
# uns hier passiert: 12 von 23 Tests waren unmarked und wurden nie
# durch die Pipeline laufen.
#
# Diese Hook setzt für jeden Test ohne ``integration``/``slow``-
# Marker implizit ``unit``. Damit gilt: **default ist unit**, und
# nur Tests, die echte externe Abhängigkeiten brauchen (DB, Broker,
# Netz), müssen explizit als ``integration`` markiert werden.
def pytest_collection_modifyitems(config, items):
    for item in items:
        markers = {m.name for m in item.iter_markers()}
        # ``integration`` / ``slow`` haben Vorrang — wer explizit
        # markiert, will nicht in die unit-Lane gezogen werden.
        if "integration" not in markers and "slow" not in markers and "unit" not in markers:
            item.add_marker(pytest.mark.unit)


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    temp_path = Path(tempfile.mkdtemp())
    yield temp_path
    if temp_path.exists():
        shutil.rmtree(temp_path)


@pytest.fixture
def mock_git_url():
    """Mock Git URL for testing."""
    return "https://github.com/test-org/test-repo.git"


@pytest.fixture
def mock_tag():
    """Mock Git tag for testing."""
    return "v1.0.0"


# ----------------------------------------------------------------
# Database (integration tests)
# ----------------------------------------------------------------
@pytest.fixture(scope="session")
def pg_url():
    """The test database with the worker's tables; skips when it is not reachable."""
    import psycopg

    from app.pgq import libpq_url

    url = libpq_url(os.environ["DATABASE_URL"])
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
            conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    except psycopg.OperationalError as e:
        pytest.skip(f"no test database at DATABASE_URL: {e}")
    return url


@pytest.fixture
def pg(pg_url):
    """An autocommit connection to the test database; tables are emptied afterwards."""
    import psycopg

    with psycopg.connect(pg_url, autocommit=True) as conn:
        yield conn
        conn.execute("TRUNCATE task_events, celery_queue, tasks CASCADE")
