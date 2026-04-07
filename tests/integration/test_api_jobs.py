"""
Integration tests for the /api/v1/jobs endpoints.
"""

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import gitdr.database.models  # noqa: F401
from gitdr.api.deps import get_fernet, get_session
from gitdr.database.models import BackupDestination, GitSource
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
def source_id(test_engine, fernet_key):
    """Seed a GitSource and return its id string."""
    fernet = Fernet(fernet_key)
    with Session(test_engine) as session:
        src = GitSource(
            name="test-source",
            forge_type="github",
            base_url="https://api.github.com",
            auth_type="token",
            auth_credential=fernet.encrypt(b"tok"),
            org_or_group="myorg",
        )
        session.add(src)
        session.commit()
        session.refresh(src)
        return str(src.id)


@pytest.fixture()
def dest_id(test_engine):
    """Seed a BackupDestination and return its id string."""
    with Session(test_engine) as session:
        dest = BackupDestination(
            name="test-dest",
            dest_type="local",
            config=b"{}",
        )
        session.add(dest)
        session.commit()
        session.refresh(dest)
        return str(dest.id)


def _job_payload(source_id: str, dest_id: str, name: str = "test-job") -> dict:
    return {
        "name": name,
        "source_id": source_id,
        "destination_id": dest_id,
        "schedule_cron": "0 2 * * *",
        "backup_type": "full",
    }


# ---------------------------------------------------------------------------
# GET /api/v1/jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    def test_empty_returns_list(self, client):
        response = client.get("/api/v1/jobs/")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_returns_created_job(self, client, source_id, dest_id):
        client.post("/api/v1/jobs/", json=_job_payload(source_id, dest_id, "list-test-job"))
        response = client.get("/api/v1/jobs/")
        assert any(j["name"] == "list-test-job" for j in response.json())


# ---------------------------------------------------------------------------
# POST /api/v1/jobs
# ---------------------------------------------------------------------------


class TestCreateJob:
    def test_creates_job(self, client, source_id, dest_id):
        response = client.post("/api/v1/jobs/", json=_job_payload(source_id, dest_id))
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "test-job"
        assert data["schedule_cron"] == "0 2 * * *"
        assert data["backup_type"] == "full"
        assert data["enabled"] is True

    def test_job_starts_enabled_by_default(self, client, source_id, dest_id):
        # The create schema has no `enabled` field; jobs always start enabled.
        payload = _job_payload(source_id, dest_id, "default-enabled-job")
        response = client.post("/api/v1/jobs/", json=payload)
        assert response.status_code == 201
        assert response.json()["enabled"] is True

    def test_job_has_id(self, client, source_id, dest_id):
        response = client.post("/api/v1/jobs/", json=_job_payload(source_id, dest_id, "id-job"))
        assert "id" in response.json()


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/{id}
# ---------------------------------------------------------------------------


class TestGetJob:
    def test_returns_job(self, client, source_id, dest_id):
        create = client.post("/api/v1/jobs/", json=_job_payload(source_id, dest_id, "getme"))
        jid = create.json()["id"]
        response = client.get(f"/api/v1/jobs/{jid}")
        assert response.status_code == 200
        assert response.json()["id"] == jid

    def test_unknown_id_returns_404(self, client):
        response = client.get(f"/api/v1/jobs/{uuid4()}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/v1/jobs/{id}
# ---------------------------------------------------------------------------


class TestUpdateJob:
    def test_updates_schedule(self, client, source_id, dest_id):
        create = client.post("/api/v1/jobs/", json=_job_payload(source_id, dest_id, "updatable"))
        jid = create.json()["id"]
        response = client.put(f"/api/v1/jobs/{jid}", json={"schedule_cron": "0 3 * * *"})
        assert response.status_code == 200
        assert response.json()["schedule_cron"] == "0 3 * * *"

    def test_toggle_enabled(self, client, source_id, dest_id):
        create = client.post("/api/v1/jobs/", json=_job_payload(source_id, dest_id, "toggle-job"))
        jid = create.json()["id"]
        response = client.put(f"/api/v1/jobs/{jid}", json={"enabled": False})
        assert response.status_code == 200
        assert response.json()["enabled"] is False

    def test_unknown_id_returns_404(self, client):
        response = client.put(f"/api/v1/jobs/{uuid4()}", json={"name": "x"})
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/jobs/{id}
# ---------------------------------------------------------------------------


class TestDeleteJob:
    def test_deletes_job(self, client, source_id, dest_id):
        create = client.post("/api/v1/jobs/", json=_job_payload(source_id, dest_id, "deleteme-job"))
        jid = create.json()["id"]
        assert client.delete(f"/api/v1/jobs/{jid}").status_code == 204
        assert client.get(f"/api/v1/jobs/{jid}").status_code == 404

    def test_unknown_id_returns_404(self, client):
        assert client.delete(f"/api/v1/jobs/{uuid4()}").status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/{id}/run
# ---------------------------------------------------------------------------


class TestTriggerJob:
    def test_enabled_job_returns_202(self, client, source_id, dest_id):
        create = client.post("/api/v1/jobs/", json=_job_payload(source_id, dest_id, "triggerable"))
        jid = create.json()["id"]
        response = client.post(f"/api/v1/jobs/{jid}/run")
        assert response.status_code == 202

    def test_disabled_job_returns_409(self, client, source_id, dest_id):
        create = client.post(
            "/api/v1/jobs/", json=_job_payload(source_id, dest_id, "disabled-trigger")
        )
        jid = create.json()["id"]
        # Disable the job via the update endpoint, then try to trigger it
        client.put(f"/api/v1/jobs/{jid}", json={"enabled": False})
        response = client.post(f"/api/v1/jobs/{jid}/run")
        assert response.status_code == 409

    def test_unknown_job_returns_404(self, client):
        response = client.post(f"/api/v1/jobs/{uuid4()}/run")
        assert response.status_code == 404
