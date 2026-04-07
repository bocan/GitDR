"""
Unit tests for gitdr.services.discovery.

All HTTP calls are mocked using unittest.mock; no real forge connections made.
Tests cover GitHub, GitLab, Azure DevOps, Bitbucket, the dispatcher, upsert
logic, and the run_discovery top-level function.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

import gitdr.database.models  # noqa: F401 — populate SQLModel.metadata
from gitdr.database.models import GitSource, Repository
from gitdr.services.discovery import (
    DiscoveredRepo,
    _discover_azure_devops,
    _discover_bitbucket,
    _discover_github,
    _discover_gitlab,
    discover_repos,
    run_discovery,
    upsert_repos,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(**kwargs: Any) -> GitSource:
    """Return an unsaved GitSource with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "name": "test-source",
        "forge_type": "github",
        "base_url": "https://api.github.com",
        "auth_type": "pat",
        "auth_credential": b"encrypted-token",
        "org_or_group": "myorg",
        "verify_ssl": True,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    src = GitSource.model_validate(defaults)
    return src


def _make_mock_client(*response_sequence: MagicMock) -> MagicMock:
    """
    Return a MagicMock that acts as an async httpx.AsyncClient context manager.

    ``get`` calls return responses from *response_sequence* in order.
    """
    client = MagicMock()
    client.get = AsyncMock(side_effect=list(response_sequence))
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


def _resp(data: Any, link: str = "", status_code: int = 200) -> MagicMock:
    """Build a mock httpx response."""
    r = MagicMock()
    r.json = MagicMock(return_value=data)
    r.raise_for_status = MagicMock()
    r.headers = MagicMock()
    r.headers.get = MagicMock(return_value=link)
    r.status_code = status_code
    return r


def _make_in_memory_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture()
def mem_engine():
    engine = _make_in_memory_engine()
    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------
# GitHub discovery
# ---------------------------------------------------------------------------


class TestDiscoverGithub:
    async def test_org_repos_single_page(self):
        raw = [
            {
                "full_name": "myorg/api",
                "clone_url": "https://github.com/myorg/api.git",
                "default_branch": "main",
                "description": "The API",
                "archived": False,
            },
            {
                "full_name": "myorg/web",
                "clone_url": "https://github.com/myorg/web.git",
                "default_branch": "main",
                "description": None,
                "archived": True,
            },
        ]
        source = _make_source(org_or_group="myorg")
        mock_client = _make_mock_client(_resp(raw))

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_github(source, "token123")

        assert len(result) == 2
        assert result[0].name == "myorg/api"
        assert result[0].is_archived is False
        assert result[1].name == "myorg/web"
        assert result[1].is_archived is True

    async def test_org_repos_pagination(self):
        page1 = [
            {
                "full_name": "myorg/repo1",
                "clone_url": "https://github.com/myorg/repo1.git",
                "default_branch": "main",
                "description": None,
                "archived": False,
            }
        ]
        page2 = [
            {
                "full_name": "myorg/repo2",
                "clone_url": "https://github.com/myorg/repo2.git",
                "default_branch": "dev",
                "description": "Repo 2",
                "archived": False,
            }
        ]
        # First response has a Link: next header; second has no Link
        r1 = _resp(page1, link='<https://api.github.com/orgs/myorg/repos?page=2>; rel="next"')
        r2 = _resp(page2)
        source = _make_source(org_or_group="myorg")
        mock_client = _make_mock_client(r1, r2)

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_github(source, "tok")

        assert len(result) == 2
        assert result[1].name == "myorg/repo2"

    async def test_user_repos_when_no_org(self):
        raw = [
            {
                "full_name": "user/personal",
                "clone_url": "https://github.com/user/personal.git",
                "default_branch": "main",
                "description": None,
                "archived": False,
            }
        ]
        source = _make_source(org_or_group=None)
        mock_client = _make_mock_client(_resp(raw))

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_github(source, "tok")

        assert len(result) == 1
        assert result[0].name == "user/personal"

    async def test_falls_back_to_user_on_404(self):
        """If the org endpoint returns 404, fall back to the user endpoint."""
        from httpx import HTTPStatusError, Request, Response

        user_raw = [
            {
                "full_name": "alice/repo",
                "clone_url": "https://github.com/alice/repo.git",
                "default_branch": "main",
                "description": None,
                "archived": False,
            }
        ]
        # First call raises 404; second call succeeds
        four_oh_four = HTTPStatusError(
            "404",
            request=Request("GET", "https://api.github.com"),
            response=Response(404),
        )
        r404 = MagicMock()
        r404.raise_for_status = MagicMock(side_effect=four_oh_four)
        r404.status_code = 404

        source = _make_source(org_or_group="alice")
        mock_client = _make_mock_client(r404, _resp(user_raw))

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_github(source, "tok")

        assert result[0].name == "alice/repo"

    async def test_missing_default_branch_defaults_to_main(self):
        raw = [
            {
                "full_name": "org/repo",
                "clone_url": "https://github.com/org/repo.git",
                "default_branch": None,
                "description": None,
                "archived": False,
            }
        ]
        source = _make_source(org_or_group="org")
        mock_client = _make_mock_client(_resp(raw))

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_github(source, "tok")

        assert result[0].default_branch == "main"


# ---------------------------------------------------------------------------
# GitLab discovery
# ---------------------------------------------------------------------------


class TestDiscoverGitlab:
    async def test_group_projects(self):
        raw = [
            {
                "path_with_namespace": "mygroup/backend",
                "http_url_to_repo": "https://gitlab.com/mygroup/backend.git",
                "default_branch": "main",
                "description": "Backend service",
                "archived": False,
            }
        ]
        source = _make_source(
            forge_type="gitlab", base_url="https://gitlab.com", org_or_group="mygroup"
        )
        mock_client = _make_mock_client(_resp(raw), _resp([]))

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_gitlab(source, "tok")

        assert len(result) == 1
        assert result[0].name == "mygroup/backend"

    async def test_empty_page_stops_pagination(self):
        raw = [
            {
                "path_with_namespace": "g/r",
                "http_url_to_repo": "https://gitlab.com/g/r.git",
                "default_branch": "main",
                "description": None,
                "archived": False,
            }
        ]
        source = _make_source(forge_type="gitlab", base_url="https://gitlab.com", org_or_group="g")
        # Page 1 has data, page 2 is empty → stop
        mock_client = _make_mock_client(_resp(raw), _resp([]))

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_gitlab(source, "tok")

        assert len(result) == 1


# ---------------------------------------------------------------------------
# Azure DevOps discovery
# ---------------------------------------------------------------------------


class TestDiscoverAzureDevops:
    async def test_single_project(self):
        repos_payload = {
            "value": [
                {
                    "name": "MyService",
                    "remoteUrl": "https://dev.azure.com/myorg/MyProject/_git/MyService",
                    "defaultBranch": "refs/heads/main",
                    "isDisabled": False,
                }
            ]
        }
        source = _make_source(
            forge_type="azure_devops",
            base_url="https://dev.azure.com",
            org_or_group="myorg/MyProject",
        )
        mock_client = _make_mock_client(_resp(repos_payload))

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_azure_devops(source, "tok")

        assert len(result) == 1
        assert result[0].name == "myorg/MyProject/MyService"
        assert result[0].default_branch == "main"
        assert result[0].is_archived is False

    async def test_org_level_lists_all_projects(self):
        projects_payload = {"value": [{"name": "Proj1"}, {"name": "Proj2"}]}
        repos_proj1 = {
            "value": [
                {
                    "name": "RepoA",
                    "remoteUrl": "https://dev.azure.com/org/Proj1/_git/RepoA",
                    "defaultBranch": "refs/heads/main",
                    "isDisabled": False,
                }
            ]
        }
        repos_proj2 = {
            "value": [
                {
                    "name": "RepoB",
                    "remoteUrl": "https://dev.azure.com/org/Proj2/_git/RepoB",
                    "defaultBranch": "refs/heads/develop",
                    "isDisabled": True,
                }
            ]
        }
        source = _make_source(
            forge_type="azure_devops",
            base_url="https://dev.azure.com",
            org_or_group="myorg",
        )
        mock_client = _make_mock_client(
            _resp(projects_payload),
            _resp(repos_proj1),
            _resp(repos_proj2),
        )

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_azure_devops(source, "tok")

        assert len(result) == 2
        names = {r.name for r in result}
        assert "myorg/Proj1/RepoA" in names
        assert "myorg/Proj2/RepoB" in names
        archived = {r.name: r.is_archived for r in result}
        assert archived["myorg/Proj2/RepoB"] is True

    async def test_requires_org_or_group(self):
        source = _make_source(forge_type="azure_devops", org_or_group=None)
        with pytest.raises(ValueError, match="org_or_group is required"):
            await _discover_azure_devops(source, "tok")

    async def test_bare_refs_heads_prefix_stripped(self):
        repos_payload = {
            "value": [
                {
                    "name": "Repo",
                    "remoteUrl": "https://dev.azure.com/o/p/_git/Repo",
                    "defaultBranch": "refs/heads/release/1.0",
                    "isDisabled": False,
                }
            ]
        }
        source = _make_source(forge_type="azure_devops", org_or_group="o/p")
        mock_client = _make_mock_client(_resp(repos_payload))

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_azure_devops(source, "tok")

        assert result[0].default_branch == "release/1.0"


# ---------------------------------------------------------------------------
# Bitbucket discovery
# ---------------------------------------------------------------------------


class TestDiscoverBitbucket:
    async def test_workspace_repos(self):
        raw = {
            "values": [
                {
                    "full_name": "myws/backend",
                    "links": {
                        "clone": [
                            {"name": "https", "href": "https://bitbucket.org/myws/backend.git"}
                        ]
                    },
                    "mainbranch": {"name": "main"},
                    "description": "Backend",
                }
            ],
            "next": None,
        }
        source = _make_source(
            forge_type="bitbucket", base_url="https://api.bitbucket.org/2.0", org_or_group="myws"
        )
        mock_client = _make_mock_client(_resp(raw))

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_bitbucket(source, "tok")

        assert len(result) == 1
        assert result[0].name == "myws/backend"
        assert result[0].clone_url == "https://bitbucket.org/myws/backend.git"
        assert result[0].default_branch == "main"
        assert result[0].is_archived is False

    async def test_pagination_via_next_url(self):
        page1 = {
            "values": [
                {
                    "full_name": "ws/r1",
                    "links": {
                        "clone": [{"name": "https", "href": "https://bitbucket.org/ws/r1.git"}]
                    },
                    "mainbranch": {"name": "main"},
                    "description": None,
                }
            ],
            "next": "https://api.bitbucket.org/2.0/repositories/ws?page=2",
        }
        page2 = {
            "values": [
                {
                    "full_name": "ws/r2",
                    "links": {
                        "clone": [{"name": "https", "href": "https://bitbucket.org/ws/r2.git"}]
                    },
                    "mainbranch": {"name": "main"},
                    "description": None,
                }
            ],
            # No "next" → stop
        }
        source = _make_source(forge_type="bitbucket", org_or_group="ws")
        mock_client = _make_mock_client(_resp(page1), _resp(page2))

        with patch("gitdr.services.discovery.httpx.AsyncClient", return_value=mock_client):
            result = await _discover_bitbucket(source, "tok")

        assert len(result) == 2

    async def test_requires_workspace(self):
        source = _make_source(forge_type="bitbucket", org_or_group=None)
        with pytest.raises(ValueError, match="workspace"):
            await _discover_bitbucket(source, "tok")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDiscoverRepos:
    async def test_dispatches_github(self):
        source = _make_source(forge_type="github", org_or_group="org")
        with patch(
            "gitdr.services.discovery._discover_github",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_fn:
            await discover_repos(source, "tok")
            mock_fn.assert_awaited_once()

    async def test_dispatches_gitlab(self):
        source = _make_source(forge_type="gitlab")
        with patch(
            "gitdr.services.discovery._discover_gitlab",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_fn:
            await discover_repos(source, "tok")
            mock_fn.assert_awaited_once()

    async def test_unknown_forge_raises(self):
        source = _make_source(forge_type="gitea")
        with pytest.raises(ValueError, match="not supported"):
            await discover_repos(source, "tok")


# ---------------------------------------------------------------------------
# upsert_repos
# ---------------------------------------------------------------------------


class TestUpsertRepos:
    def test_inserts_new_repos(self, mem_engine):
        source_id = uuid4()
        discovered = [
            DiscoveredRepo(
                name="org/repo-a",
                clone_url="https://github.com/org/repo-a.git",
                default_branch="main",
                description="Repo A",
                is_archived=False,
            )
        ]
        with Session(mem_engine) as session:
            new_count, updated_count = upsert_repos(source_id, discovered, session)

        assert new_count == 1
        assert updated_count == 0

    def test_updates_existing_repos(self, mem_engine):
        source_id = uuid4()

        # Seed an existing repo
        with Session(mem_engine) as session:
            repo = Repository(
                source_id=source_id,
                repo_name="org/repo-a",
                clone_url="https://github.com/org/repo-a.git",
                default_branch="master",
                description=None,
                is_archived=False,
            )
            session.add(repo)
            session.commit()

        # Discovery returns the same repo with an updated branch
        discovered = [
            DiscoveredRepo(
                name="org/repo-a",
                clone_url="https://github.com/org/repo-a.git",
                default_branch="main",
                description="Now has a description",
                is_archived=False,
            )
        ]
        with Session(mem_engine) as session:
            new_count, updated_count = upsert_repos(source_id, discovered, session)
        assert new_count == 0
        assert updated_count == 1

        # Verify DB update
        with Session(mem_engine) as session:
            repo = session.exec(
                __import__("sqlmodel", fromlist=["select"]).select(Repository)
            ).one()
            assert repo.default_branch == "main"
            assert repo.description == "Now has a description"

    def test_mixed_new_and_updated(self, mem_engine):
        source_id = uuid4()

        with Session(mem_engine) as session:
            session.add(
                Repository(
                    source_id=source_id,
                    repo_name="org/existing",
                    clone_url="https://github.com/org/existing.git",
                    default_branch="main",
                    description=None,
                    is_archived=False,
                )
            )
            session.commit()

        discovered = [
            DiscoveredRepo(
                "org/existing", "https://github.com/org/existing.git", "main", None, False
            ),
            DiscoveredRepo(
                "org/brand-new", "https://github.com/org/brand-new.git", "main", None, False
            ),
        ]
        with Session(mem_engine) as session:
            new_count, updated_count = upsert_repos(source_id, discovered, session)

        assert new_count == 1
        assert updated_count == 1


# ---------------------------------------------------------------------------
# run_discovery (integration-style, no real HTTP)
# ---------------------------------------------------------------------------


class TestRunDiscovery:
    async def test_full_flow(self, mem_engine):
        source_id = uuid4()

        # Seed a GitSource
        from cryptography.fernet import Fernet

        fernet_key = Fernet.generate_key()
        fernet = Fernet(fernet_key)
        encrypted_token = fernet.encrypt(b"mytoken")

        with Session(mem_engine) as session:
            source = GitSource(
                id=source_id,
                name="test",
                forge_type="github",
                base_url="https://api.github.com",
                auth_type="pat",
                auth_credential=encrypted_token,
                org_or_group="myorg",
                verify_ssl=True,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(source)
            session.commit()

        discovered = [
            DiscoveredRepo("myorg/repo", "https://github.com/myorg/repo.git", "main", None, False)
        ]
        with patch(
            "gitdr.services.discovery.discover_repos",
            new_callable=AsyncMock,
            return_value=discovered,
        ):
            result = await run_discovery(source_id, mem_engine, fernet_key)

        assert result["new"] == 1
        assert result["updated"] == 0
        assert result["total"] == 1

        # Verify the repo is in the DB
        with Session(mem_engine) as session:
            from sqlmodel import select as sq_select

            repos = list(session.exec(sq_select(Repository)).all())
            assert len(repos) == 1
            assert repos[0].repo_name == "myorg/repo"

    async def test_missing_source_raises(self, mem_engine):
        from cryptography.fernet import Fernet

        fernet_key = Fernet.generate_key()
        with pytest.raises(ValueError, match="not found"):
            await run_discovery(uuid4(), mem_engine, fernet_key)
