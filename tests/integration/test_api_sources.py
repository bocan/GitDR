"""
Integration tests for the /api/v1/sources endpoints.

Uses FastAPI's TestClient with dependency overrides so no real SQLCipher
engine or Fernet key derivation is needed.
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
# Shared fixtures
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
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /api/v1/sources
# ---------------------------------------------------------------------------


class TestListSources:
    def test_empty_list(self, client):
        response = client.get("/api/v1/sources/")
        assert response.status_code == 200
        assert response.json() == []

    def test_returns_created_source(self, client):
        client.post(
            "/api/v1/sources/",
            json={
                "name": "my-gh",
                "forge_type": "github",
                "base_url": "https://api.github.com",
                "auth_type": "token",
                "auth_credential": "secret-token",
                "org_or_group": "myorg",
            },
        )
        response = client.get("/api/v1/sources/")
        assert response.status_code == 200
        names = [s["name"] for s in response.json()]
        assert "my-gh" in names


# ---------------------------------------------------------------------------
# POST /api/v1/sources
# ---------------------------------------------------------------------------


class TestCreateSource:
    def test_creates_source(self, client):
        payload = {
            "name": "test-source",
            "forge_type": "github",
            "base_url": "https://api.github.com",
            "auth_type": "token",
            "auth_credential": "tok",
            "org_or_group": "acme",
        }
        response = client.post("/api/v1/sources/", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "test-source"
        assert data["forge_type"] == "github"
        assert data["org_or_group"] == "acme"
        assert "id" in data

    def test_duplicate_name_returns_error(self, client):
        payload = {
            "name": "dup-source",
            "forge_type": "gitlab",
            "base_url": "https://gitlab.com",
            "auth_type": "token",
            "auth_credential": "tok",
        }
        client.post("/api/v1/sources/", json=payload)
        response = client.post("/api/v1/sources/", json=payload)
        assert response.status_code in (409, 422, 500)

    def test_credential_not_exposed_in_response(self, client):
        payload = {
            "name": "secure-src",
            "forge_type": "github",
            "base_url": "https://api.github.com",
            "auth_type": "token",
            "auth_credential": "my-secret-token",
        }
        response = client.post("/api/v1/sources/", json=payload)
        body = response.text
        assert "my-secret-token" not in body


# ---------------------------------------------------------------------------
# GET /api/v1/sources/{id}
# ---------------------------------------------------------------------------


class TestGetSource:
    def test_returns_source(self, client):
        create = client.post(
            "/api/v1/sources/",
            json={
                "name": "fetchable",
                "forge_type": "bitbucket",
                "base_url": "https://api.bitbucket.org",
                "auth_type": "token",
                "auth_credential": "tok",
            },
        )
        sid = create.json()["id"]
        response = client.get(f"/api/v1/sources/{sid}")
        assert response.status_code == 200
        assert response.json()["id"] == sid

    def test_unknown_id_returns_404(self, client):
        response = client.get(f"/api/v1/sources/{uuid4()}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/v1/sources/{id}
# ---------------------------------------------------------------------------


class TestUpdateSource:
    def test_updates_name(self, client):
        create = client.post(
            "/api/v1/sources/",
            json={
                "name": "orig-name",
                "forge_type": "github",
                "base_url": "https://api.github.com",
                "auth_type": "token",
                "auth_credential": "tok",
            },
        )
        sid = create.json()["id"]
        response = client.put(f"/api/v1/sources/{sid}", json={"name": "new-name"})
        assert response.status_code == 200
        assert response.json()["name"] == "new-name"

    def test_unknown_id_returns_404(self, client):
        response = client.put(f"/api/v1/sources/{uuid4()}", json={"name": "x"})
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/sources/{id}
# ---------------------------------------------------------------------------


class TestDeleteSource:
    def test_deletes_source(self, client):
        create = client.post(
            "/api/v1/sources/",
            json={
                "name": "to-delete",
                "forge_type": "github",
                "base_url": "https://api.github.com",
                "auth_type": "token",
                "auth_credential": "tok",
            },
        )
        sid = create.json()["id"]
        response = client.delete(f"/api/v1/sources/{sid}")
        assert response.status_code == 204
        assert client.get(f"/api/v1/sources/{sid}").status_code == 404

    def test_unknown_id_returns_404(self, client):
        response = client.delete(f"/api/v1/sources/{uuid4()}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/sources/{id}/discover
# ---------------------------------------------------------------------------


class TestTriggerDiscovery:
    def test_returns_200_with_list(self, client):
        create = client.post(
            "/api/v1/sources/",
            json={
                "name": "discover-me",
                "forge_type": "github",
                "base_url": "https://api.github.com",
                "auth_type": "pat",
                "auth_credential": "tok",
            },
        )
        sid = create.json()["id"]
        from unittest.mock import AsyncMock, patch

        with patch(
            "gitdr.api.routers.sources.discover_repos",
            new=AsyncMock(return_value=[]),
        ):
            response = client.post(f"/api/v1/sources/{sid}/discover")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_unknown_source_returns_404(self, client):
        response = client.post(f"/api/v1/sources/{uuid4()}/discover")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/sources/test-connection
# ---------------------------------------------------------------------------


class TestTestConnectionProbe:
    def test_success(self, client):
        from unittest.mock import AsyncMock, patch

        with patch(
            "gitdr.api.routers.sources.test_connection",
            new=AsyncMock(return_value="Authenticated as testuser"),
        ):
            response = client.post(
                "/api/v1/sources/test-connection",
                json={
                    "forge_type": "github",
                    "base_url": "https://api.github.com",
                    "auth_credential": "ghp_fake",
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "testuser" in data["message"]

    def test_wrong_credentials_returns_400(self, client):
        from unittest.mock import AsyncMock, patch

        import httpx

        mock_response = httpx.Response(401, request=httpx.Request("GET", "https://x"))
        with patch(
            "gitdr.api.routers.sources.test_connection",
            new=AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "", request=mock_response.request, response=mock_response
                )
            ),
        ):
            response = client.post(
                "/api/v1/sources/test-connection",
                json={
                    "forge_type": "github",
                    "base_url": "https://api.github.com",
                    "auth_credential": "bad_token",
                },
            )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/v1/sources/{id}/test-connection
# ---------------------------------------------------------------------------


class TestTestConnectionExisting:
    def test_existing_source_success(self, client):
        create = client.post(
            "/api/v1/sources/",
            json={
                "name": "test-conn-src",
                "forge_type": "github",
                "base_url": "https://api.github.com",
                "auth_type": "pat",
                "auth_credential": "tok",
            },
        )
        sid = create.json()["id"]
        from unittest.mock import AsyncMock, patch

        with patch(
            "gitdr.api.routers.sources.test_connection",
            new=AsyncMock(return_value="Authenticated as somebody"),
        ):
            response = client.post(f"/api/v1/sources/{sid}/test-connection")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_unknown_source_returns_404(self, client):
        response = client.post(f"/api/v1/sources/{uuid4()}/test-connection")
        assert response.status_code == 404
