"""
Integration tests for the /api/v1/destinations endpoints.
"""

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import gitdr.database.models  # noqa: F401
from gitdr.api.deps import get_fernet, get_session
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


def _dest_payload(name: str = "My Dest") -> dict:
    return {
        "name": name,
        "dest_type": "local",
        "config": {"path": "/backups"},
    }


# ---------------------------------------------------------------------------
# GET /api/v1/destinations
# ---------------------------------------------------------------------------


class TestListDestinations:
    def test_returns_list(self, client):
        response = client.get("/api/v1/destinations/")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_includes_created_destination(self, client):
        client.post("/api/v1/destinations/", json=_dest_payload("list-dest"))
        response = client.get("/api/v1/destinations/")
        names = [d["name"] for d in response.json()]
        assert "list-dest" in names


# ---------------------------------------------------------------------------
# POST /api/v1/destinations
# ---------------------------------------------------------------------------


class TestCreateDestination:
    def test_returns_201(self, client):
        response = client.post("/api/v1/destinations/", json=_dest_payload())
        assert response.status_code == 201

    def test_response_has_id(self, client):
        response = client.post("/api/v1/destinations/", json=_dest_payload())
        assert "id" in response.json()

    def test_response_has_correct_name(self, client):
        response = client.post("/api/v1/destinations/", json=_dest_payload("named-dest"))
        assert response.json()["name"] == "named-dest"

    def test_config_not_exposed_as_plaintext(self, client):
        response = client.post("/api/v1/destinations/", json=_dest_payload())
        assert response.status_code == 201
        # Encrypted config must not appear in the response body at all
        body = response.text
        assert "/backups" not in body


# ---------------------------------------------------------------------------
# GET /api/v1/destinations/{id}
# ---------------------------------------------------------------------------


class TestGetDestination:
    def test_returns_destination(self, client):
        created = client.post("/api/v1/destinations/", json=_dest_payload()).json()
        response = client.get(f"/api/v1/destinations/{created['id']}")
        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    def test_unknown_id_returns_404(self, client):
        response = client.get(f"/api/v1/destinations/{uuid4()}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/v1/destinations/{id}
# ---------------------------------------------------------------------------


class TestUpdateDestination:
    def test_updates_name(self, client):
        created = client.post("/api/v1/destinations/", json=_dest_payload()).json()
        response = client.put(
            f"/api/v1/destinations/{created['id']}",
            json={"name": "updated-name"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "updated-name"

    def test_unknown_id_returns_404(self, client):
        response = client.put(
            f"/api/v1/destinations/{uuid4()}",
            json={"name": "x"},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/destinations/{id}
# ---------------------------------------------------------------------------


class TestDeleteDestination:
    def test_returns_204(self, client):
        created = client.post("/api/v1/destinations/", json=_dest_payload()).json()
        response = client.delete(f"/api/v1/destinations/{created['id']}")
        assert response.status_code == 204

    def test_gone_after_delete(self, client):
        created = client.post("/api/v1/destinations/", json=_dest_payload()).json()
        client.delete(f"/api/v1/destinations/{created['id']}")
        response = client.get(f"/api/v1/destinations/{created['id']}")
        assert response.status_code == 404

    def test_unknown_id_returns_404(self, client):
        response = client.delete(f"/api/v1/destinations/{uuid4()}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/destinations/{id}/test
# ---------------------------------------------------------------------------


class TestTestDestination:
    def test_returns_200_for_existing_dest(self, client):
        created = client.post("/api/v1/destinations/", json=_dest_payload()).json()
        response = client.post(f"/api/v1/destinations/{created['id']}/test")
        assert response.status_code == 200

    def test_response_has_status_ok(self, client):
        created = client.post("/api/v1/destinations/", json=_dest_payload()).json()
        response = client.post(f"/api/v1/destinations/{created['id']}/test")
        assert response.json()["status"] == "ok"

    def test_unknown_id_returns_404(self, client):
        response = client.post(f"/api/v1/destinations/{uuid4()}/test")
        assert response.status_code == 404
