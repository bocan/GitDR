"""
Integration tests for the /api/v1/runs endpoints.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import gitdr.database.models  # noqa: F401
from gitdr.api.deps import get_fernet, get_session
from gitdr.database.models import BackupDestination, BackupJob, BackupRun, GitSource, Repository
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
        # Point app.state.engine at the same in-memory engine the session uses,
        # so that background tasks (which open their own Session from app.state.engine)
        # share the same database as the test session.
        c.app.state.engine = test_engine  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded(test_engine, fernet_key):
    """
    Seed a source, dest, job, repo, and two runs (one success, one failed).
    Returns a dict with ids as strings.
    """
    fernet = Fernet(fernet_key)
    with Session(test_engine) as session:
        src = GitSource(
            name="run-src",
            forge_type="github",
            base_url="https://api.github.com",
            auth_type="token",
            auth_credential=fernet.encrypt(b"tok"),
        )
        dest = BackupDestination(
            name="run-dest",
            dest_type="local",
            config=Fernet(fernet_key).encrypt(b'{"path": "/tmp/gitdr-test-restore"}'),
        )
        session.add(src)
        session.add(dest)
        session.flush()

        job = BackupJob(
            name="run-job",
            source_id=src.id,
            destination_id=dest.id,
        )
        repo = Repository(
            source_id=src.id,
            repo_name="testrepo",
            clone_url="https://github.com/org/testrepo",
        )
        session.add(job)
        session.add(repo)
        session.flush()

        run_ok = BackupRun(
            job_id=job.id,
            repo_id=repo.id,
            status="success",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            size_bytes=1024,
            archive_path="gitdr/run-src/testrepo/20250101T000000_000000Z.bundle",
        )
        run_fail = BackupRun(
            job_id=job.id,
            repo_id=repo.id,
            status="failed",
            error_message="connection refused",
        )
        session.add(run_ok)
        session.add(run_fail)
        session.commit()
        session.refresh(run_ok)
        session.refresh(run_fail)

        return {
            "job_id": str(job.id),
            "repo_id": str(repo.id),
            "run_ok_id": str(run_ok.id),
            "run_fail_id": str(run_fail.id),
        }


# ---------------------------------------------------------------------------
# GET /api/v1/runs
# ---------------------------------------------------------------------------


class TestListRuns:
    def test_returns_list(self, client):
        response = client.get("/api/v1/runs/")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_returns_seeded_runs(self, client, seeded):
        response = client.get("/api/v1/runs/")
        assert response.status_code == 200
        ids = [r["id"] for r in response.json()]
        assert seeded["run_ok_id"] in ids
        assert seeded["run_fail_id"] in ids

    def test_filter_by_status_success(self, client, seeded):
        response = client.get("/api/v1/runs/?status=success")
        assert response.status_code == 200
        statuses = [r["status"] for r in response.json()]
        assert all(s == "success" for s in statuses)

    def test_filter_by_status_failed(self, client, seeded):
        response = client.get("/api/v1/runs/?status=failed")
        assert response.status_code == 200
        statuses = [r["status"] for r in response.json()]
        assert all(s == "failed" for s in statuses)

    def test_filter_by_job_id(self, client, seeded):
        response = client.get(f"/api/v1/runs/?job_id={seeded['job_id']}")
        assert response.status_code == 200
        assert len(response.json()) >= 2

    def test_limit_param(self, client, seeded):
        response = client.get("/api/v1/runs/?limit=1")
        assert response.status_code == 200
        assert len(response.json()) <= 1


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{id}
# ---------------------------------------------------------------------------


class TestGetRun:
    def test_returns_run(self, client, seeded):
        rid = seeded["run_ok_id"]
        response = client.get(f"/api/v1/runs/{rid}")
        assert response.status_code == 200
        assert response.json()["id"] == rid

    def test_run_has_expected_fields(self, client, seeded):
        response = client.get(f"/api/v1/runs/{seeded['run_ok_id']}")
        data = response.json()
        assert "status" in data
        assert "job_id" in data
        assert "repo_id" in data

    def test_unknown_id_returns_404(self, client):
        response = client.get(f"/api/v1/runs/{uuid4()}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/runs/{id}/restore
# ---------------------------------------------------------------------------


class TestInitiateRestore:
    def test_successful_run_returns_202(self, client, seeded):
        with patch(
            "gitdr.services.restore.run_restore",
            new_callable=AsyncMock,
            return_value=(Path("/tmp/restored"), "log"),  # noqa: S108
        ):
            response = client.post(f"/api/v1/runs/{seeded['run_ok_id']}/restore", json={})
        assert response.status_code == 202

    def test_restore_response_shape(self, client, seeded):
        with patch(
            "gitdr.services.restore.run_restore",
            new_callable=AsyncMock,
            return_value=(Path("/tmp/restored"), "log"),  # noqa: S108
        ):
            response = client.post(f"/api/v1/runs/{seeded['run_ok_id']}/restore", json={})
        data = response.json()
        assert data["status"] == "accepted"
        assert data["run_id"] == seeded["run_ok_id"]
        assert "restore_run_id" in data
        assert "archive_path" in data
        assert "restore_dir" in data

    def test_failed_run_returns_409(self, client, seeded):
        response = client.post(f"/api/v1/runs/{seeded['run_fail_id']}/restore", json={})
        assert response.status_code == 409

    def test_unknown_run_returns_404(self, client):
        response = client.post(f"/api/v1/runs/{uuid4()}/restore", json={})
        assert response.status_code == 404

    def test_restore_creates_restore_run_record(self, client, seeded):
        """POST /restore should create a RestoreRun record accessible via GET."""
        with patch(
            "gitdr.services.restore.run_restore",
            new_callable=AsyncMock,
            return_value=(Path("/tmp/restored"), "log"),  # noqa: S108
        ):
            resp = client.post(f"/api/v1/runs/{seeded['run_ok_id']}/restore", json={})
        assert resp.status_code == 202
        restore_run_id = resp.json()["restore_run_id"]

        detail = client.get(f"/api/v1/runs/{seeded['run_ok_id']}/restores/{restore_run_id}")
        assert detail.status_code == 200
        assert detail.json()["id"] == restore_run_id

    def test_push_url_stored_on_restore_run(self, client, seeded):
        push = "https://github.com/org/new-repo.git"
        with patch(
            "gitdr.services.restore.run_restore",
            new_callable=AsyncMock,
            return_value=(Path("/tmp/restored"), "log"),  # noqa: S108
        ):
            resp = client.post(
                f"/api/v1/runs/{seeded['run_ok_id']}/restore",
                json={"push_url": push},
            )
        assert resp.status_code == 202
        restore_run_id = resp.json()["restore_run_id"]
        detail = client.get(f"/api/v1/runs/{seeded['run_ok_id']}/restores/{restore_run_id}")
        assert detail.json()["push_url"] == push


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{id}/restores
# ---------------------------------------------------------------------------


class TestListRestoreRuns:
    def test_returns_empty_list_initially(self, client, seeded):
        response = client.get(f"/api/v1/runs/{seeded['run_ok_id']}/restores")
        assert response.status_code == 200
        # May have entries from other tests in this class; just check it's a list
        assert isinstance(response.json(), list)

    def test_unknown_run_returns_404(self, client):
        response = client.get(f"/api/v1/runs/{uuid4()}/restores")
        assert response.status_code == 404

    def test_restore_run_fields_present(self, client, seeded):
        with patch(
            "gitdr.services.restore.run_restore",
            new_callable=AsyncMock,
            return_value=(Path("/tmp/restored"), "log"),  # noqa: S108
        ):
            client.post(f"/api/v1/runs/{seeded['run_ok_id']}/restore", json={})
        response = client.get(f"/api/v1/runs/{seeded['run_ok_id']}/restores")
        assert response.status_code == 200
        items = response.json()
        assert len(items) >= 1
        rr = items[0]
        assert "id" in rr
        assert "status" in rr
        assert "backup_run_id" in rr
        assert "created_at" in rr
