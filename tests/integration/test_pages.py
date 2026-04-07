"""
Smoke tests for the HTML page routes (server-rendered Jinja2 templates).

These tests verify that each page route returns HTTP 200 and HTML content
when the database is empty, and that 404 pages work for unknown IDs.
"""

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import gitdr.database.models  # noqa: F401 — ensure metadata is populated
from gitdr.api.deps import get_fernet, get_session
from gitdr.main import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fernet_key() -> bytes:
    return Fernet.generate_key()


@pytest.fixture()
def test_engine(tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def client(test_engine, fernet_key):
    fernet = Fernet(fernet_key)

    def override_session():
        with Session(test_engine) as session:
            yield session

    def override_fernet():
        return fernet

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_fernet] = override_fernet
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper to create a source (needed by several page tests)
# ---------------------------------------------------------------------------


def _create_source(client) -> str:
    r = client.post(
        "/api/v1/sources/",
        json={
            "name": "smoke-src",
            "forge_type": "github",
            "base_url": "https://api.github.com",
            "auth_type": "token",
            "auth_credential": "tok",
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class TestDashboardPage:
    def test_returns_200(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_returns_html(self, client):
        response = client.get("/")
        assert "text/html" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Sources pages
# ---------------------------------------------------------------------------


class TestSourcesPage:
    def test_list_returns_200(self, client):
        response = client.get("/sources")
        assert response.status_code == 200

    def test_detail_returns_200(self, client):
        src_id = _create_source(client)
        response = client.get(f"/sources/{src_id}")
        assert response.status_code == 200

    def test_detail_unknown_id_returns_404(self, client):
        response = client.get(f"/sources/{uuid4()}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Destinations page
# ---------------------------------------------------------------------------


class TestDestinationsPage:
    def test_returns_200(self, client):
        response = client.get("/destinations")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Jobs pages
# ---------------------------------------------------------------------------


class TestJobsPage:
    def test_list_returns_200(self, client):
        response = client.get("/jobs")
        assert response.status_code == 200

    def test_detail_unknown_id_returns_404(self, client):
        response = client.get(f"/jobs/{uuid4()}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Runs pages
# ---------------------------------------------------------------------------


class TestRunsPage:
    def test_list_returns_200(self, client):
        response = client.get("/runs")
        assert response.status_code == 200

    def test_list_with_status_filter(self, client):
        response = client.get("/runs?status=success")
        assert response.status_code == 200

    def test_detail_unknown_id_returns_404(self, client):
        response = client.get(f"/runs/{uuid4()}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------


class TestSettingsPage:
    def test_returns_200(self, client):
        response = client.get("/settings")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Partials
# ---------------------------------------------------------------------------


class TestSourceReposPartial:
    def test_returns_200_for_known_source(self, client):
        src_id = _create_source(client)
        response = client.get(f"/partials/source-repos/{src_id}")
        assert response.status_code == 200

    def test_returns_200_for_unknown_source(self, client):
        # Partial renders empty list (no 404 — source may not exist yet during job creation)
        response = client.get(f"/partials/source-repos/{uuid4()}")
        assert response.status_code == 200
