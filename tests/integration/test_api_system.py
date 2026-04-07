"""
Integration tests for the /api/v1/system and /api/v1/repositories endpoints.
"""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import gitdr.database.models  # noqa: F401
from gitdr.api.deps import get_fernet, get_session
from gitdr.database.models import GitSource, Repository
from gitdr.main import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fernet_key() -> bytes:
    return Fernet.generate_key()


@pytest.fixture()
def test_engine():
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
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded_repo(test_engine, fernet_key):
    """Seed a source + repo; return repo id."""
    fernet = Fernet(fernet_key)
    with Session(test_engine) as session:
        src = GitSource(
            name="sys-src",
            forge_type="github",
            base_url="https://api.github.com",
            auth_type="token",
            auth_credential=fernet.encrypt(b"tok"),
        )
        session.add(src)
        session.flush()

        repo = Repository(
            source_id=src.id,
            repo_name="testrepo",
            clone_url="https://github.com/org/testrepo",
        )
        session.add(repo)
        session.commit()
        session.refresh(repo)
        return str(repo.id)


# ---------------------------------------------------------------------------
# GET /api/v1/system/health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_returns_200(self, client):
        response = client.get("/api/v1/system/health")
        assert response.status_code == 200

    def test_status_is_ok(self, client):
        response = client.get("/api/v1/system/health")
        assert response.json()["status"] == "ok"

    def test_service_is_gitdr(self, client):
        response = client.get("/api/v1/system/health")
        assert response.json()["service"] == "gitdr"


# ---------------------------------------------------------------------------
# GET /api/v1/system/stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_returns_200(self, client):
        response = client.get("/api/v1/system/stats")
        assert response.status_code == 200

    def test_has_expected_fields(self, client):
        data = client.get("/api/v1/system/stats").json()
        expected = {
            "total_sources",
            "total_repos",
            "total_destinations",
            "total_jobs",
            "total_runs",
            "successful_runs",
            "failed_runs",
        }
        assert expected.issubset(data.keys())

    def test_numeric_values_are_non_negative(self, client):
        data = client.get("/api/v1/system/stats").json()
        for field in (
            "total_sources",
            "total_repos",
            "total_destinations",
            "total_jobs",
            "total_runs",
            "successful_runs",
            "failed_runs",
        ):
            assert data[field] >= 0


# ---------------------------------------------------------------------------
# GET /api/v1/repositories
# ---------------------------------------------------------------------------


class TestListRepositories:
    def test_returns_list(self, client):
        response = client.get("/api/v1/repositories/")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_includes_seeded_repo(self, client, seeded_repo):
        response = client.get("/api/v1/repositories/")
        ids = [r["id"] for r in response.json()]
        assert seeded_repo in ids
