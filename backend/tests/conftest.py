"""Pytest fixtures.

Env vars are set BEFORE any ``app.*`` module is imported so the database
engine, JWT secret, and settings all reflect the test configuration at
module-load time. Reloading after the fact is unreliable because router
modules capture references to ``get_db`` and ``SessionLocal`` at import
time.
"""

import os
import tempfile
from pathlib import Path

# Set env vars before importing the app — must happen at module load.
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="ivc-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_DB_DIR / 'test.db'}")
os.environ.setdefault("JWT_SECRET", "test-secret-please-do-not-use-in-prod-32+")
os.environ.setdefault("ENV", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="function")
def client():
    # Fresh DB for every test: drop and recreate all tables. Disposing the
    # engine first closes any pooled connections that still hold the previous
    # SQLite file open, which would otherwise lead to "readonly database"
    # errors when the file is re-created underneath the open handles.
    from app.db import Base, engine  # noqa: WPS433 — deferred to honour env

    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    from app.main import app  # noqa: WPS433
    with TestClient(app) as c:
        yield c
