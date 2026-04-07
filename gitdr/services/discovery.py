"""
Forge API discovery clients for GitDR.

Discovers repositories from GitHub, GitLab, Azure DevOps, and Bitbucket
via their respective REST APIs, handling pagination automatically.

Security notes:
- Tokens are only held in memory during the discovery call; never logged.
- All forge API calls use HTTPS (enforced by the forge base_url stored at
  source creation time).  ``verify_ssl`` defaults to True and is only False
  for self-hosted forges with self-signed certificates.
- HTTP 429 / 503 responses are surfaced as exceptions; callers should retry
  with backoff (APScheduler retry policy covers the scheduled case).
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from gitdr.database.models import GitSource, Repository

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_MAX_PAGES = 200  # hard cap against infinite pagination loops


# ---------------------------------------------------------------------------
# Shared result type
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredRepo:
    """A single repository returned by a forge API."""

    name: str  # full name, e.g. "org/repo-name"
    clone_url: str  # HTTPS clone URL
    default_branch: str
    description: str | None
    is_archived: bool


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


async def _github_pages(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Follow Link-header pagination and collect all items."""
    items: list[dict[str, Any]] = []
    next_url: str | None = url
    page = 0
    while next_url and page < _MAX_PAGES:
        r = await client.get(next_url, params=params if page == 0 else {})
        r.raise_for_status()
        items.extend(r.json())
        next_url = None
        for part in r.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
                break
        page += 1
    return items


async def _discover_github(source: GitSource, token: str) -> list[DiscoveredRepo]:
    base = (source.base_url or "https://api.github.com").rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(
        headers=headers,
        verify=source.verify_ssl,
        timeout=_TIMEOUT,
    ) as client:
        if source.org_or_group:
            try:
                raw = await _github_pages(
                    client,
                    f"{base}/orgs/{source.org_or_group}/repos",
                    {"per_page": 100, "type": "all"},
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # Not an org — try as a user
                    raw = await _github_pages(
                        client,
                        f"{base}/users/{source.org_or_group}/repos",
                        {"per_page": 100, "type": "all"},
                    )
                else:
                    raise
        else:
            # Authenticated user's repos (owner + org member)
            raw = await _github_pages(
                client,
                f"{base}/user/repos",
                {"per_page": 100, "affiliation": "owner,organization_member"},
            )

    return [
        DiscoveredRepo(
            name=r["full_name"],
            clone_url=r["clone_url"],
            default_branch=r.get("default_branch") or "main",
            description=r.get("description"),
            is_archived=bool(r.get("archived", False)),
        )
        for r in raw
    ]


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


async def _gitlab_pages(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Page through GitLab's numeric page/per_page pagination."""
    items: list[dict[str, Any]] = []
    page = 1
    while page <= _MAX_PAGES:
        r = await client.get(url, params={**params, "page": page, "per_page": 100})
        r.raise_for_status()
        data: list[dict[str, Any]] = r.json()
        if not data:
            break
        items.extend(data)
        if len(data) < 100:
            break
        page += 1
    return items


async def _discover_gitlab(source: GitSource, token: str) -> list[DiscoveredRepo]:
    base = (source.base_url or "https://gitlab.com").rstrip("/")
    headers = {"PRIVATE-TOKEN": token}
    async with httpx.AsyncClient(
        headers=headers,
        verify=source.verify_ssl,
        timeout=_TIMEOUT,
    ) as client:
        if source.org_or_group:
            # URL-encode slashes in nested group paths
            encoded = source.org_or_group.replace("/", "%2F")
            raw = await _gitlab_pages(
                client,
                f"{base}/api/v4/groups/{encoded}/projects",
                {"include_subgroups": "true", "with_shared": "false"},
            )
        else:
            raw = await _gitlab_pages(
                client,
                f"{base}/api/v4/projects",
                {"membership": "true"},
            )

    return [
        DiscoveredRepo(
            name=r["path_with_namespace"],
            clone_url=r["http_url_to_repo"],
            default_branch=r.get("default_branch") or "main",
            description=r.get("description"),
            is_archived=bool(r.get("archived", False)),
        )
        for r in raw
    ]


# ---------------------------------------------------------------------------
# Azure DevOps
# ---------------------------------------------------------------------------


def _azure_default_branch(repo: dict[str, Any]) -> str:
    """Strip the 'refs/heads/' prefix from Azure's defaultBranch field."""
    raw = repo.get("defaultBranch") or "refs/heads/main"
    return raw.removeprefix("refs/heads/")


async def _discover_azure_devops(source: GitSource, token: str) -> list[DiscoveredRepo]:
    """
    Discover repos from Azure DevOps.

    ``org_or_group`` must be ``"org"`` (lists all projects then all repos)
    or ``"org/project"`` (lists only repos in that one project).

    Authentication uses HTTP Basic auth with an empty username and the
    Personal Access Token as the password.
    """
    if not source.org_or_group:
        raise ValueError("org_or_group is required for Azure DevOps — use 'org' or 'org/project'")

    base = (source.base_url or "https://dev.azure.com").rstrip("/")
    creds = base64.b64encode(f":{token}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(
        headers=headers,
        verify=source.verify_ssl,
        timeout=_TIMEOUT,
    ) as client:
        if "/" in source.org_or_group:
            org, project = source.org_or_group.split("/", 1)
            r = await client.get(
                f"{base}/{org}/{project}/_apis/git/repositories",
                params={"api-version": "7.0"},
            )
            r.raise_for_status()
            repos = r.json().get("value", [])
            return [
                DiscoveredRepo(
                    name=f"{org}/{project}/{repo['name']}",
                    clone_url=repo["remoteUrl"],
                    default_branch=_azure_default_branch(repo),
                    description=None,
                    is_archived=bool(repo.get("isDisabled", False)),
                )
                for repo in repos
            ]
        else:
            org = source.org_or_group
            r = await client.get(
                f"{base}/{org}/_apis/projects",
                params={"api-version": "7.0"},
            )
            r.raise_for_status()
            projects = r.json().get("value", [])
            results: list[DiscoveredRepo] = []
            for proj in projects:
                proj_name: str = proj["name"]
                r2 = await client.get(
                    f"{base}/{org}/{proj_name}/_apis/git/repositories",
                    params={"api-version": "7.0"},
                )
                if r2.status_code == 200:
                    for repo in r2.json().get("value", []):
                        results.append(
                            DiscoveredRepo(
                                name=f"{org}/{proj_name}/{repo['name']}",
                                clone_url=repo["remoteUrl"],
                                default_branch=_azure_default_branch(repo),
                                description=None,
                                is_archived=bool(repo.get("isDisabled", False)),
                            )
                        )
            return results


# ---------------------------------------------------------------------------
# Bitbucket
# ---------------------------------------------------------------------------


async def _discover_bitbucket(source: GitSource, token: str) -> list[DiscoveredRepo]:
    """
    Discover repos from Bitbucket Cloud.

    ``org_or_group`` is the workspace slug (required).
    Pagination follows the ``next`` URL returned in each page's JSON.
    """
    if not source.org_or_group:
        raise ValueError("org_or_group (workspace slug) is required for Bitbucket")

    base = (source.base_url or "https://api.bitbucket.org/2.0").rstrip("/")
    workspace = source.org_or_group
    items: list[dict[str, Any]] = []
    next_url: str | None = f"{base}/repositories/{workspace}"
    params: dict[str, Any] = {"pagelen": 100}
    page = 0

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        verify=source.verify_ssl,
        timeout=_TIMEOUT,
    ) as client:
        while next_url and page < _MAX_PAGES:
            r = await client.get(next_url, params=params if page == 0 else {})
            r.raise_for_status()
            data = r.json()
            items.extend(data.get("values", []))
            next_url = data.get("next")
            page += 1

    def _bb_clone_url(repo: dict[str, Any]) -> str:
        for link in repo.get("links", {}).get("clone", []):
            if link.get("name") == "https":
                return str(link["href"])
        return ""

    def _bb_default_branch(repo: dict[str, Any]) -> str:
        mb = repo.get("mainbranch")
        if isinstance(mb, dict):
            return str(mb.get("name", "main"))
        return "main"

    return [
        DiscoveredRepo(
            name=r["full_name"],
            clone_url=_bb_clone_url(r),
            default_branch=_bb_default_branch(r),
            description=r.get("description"),
            is_archived=False,  # Bitbucket has no archived concept at repo level
        )
        for r in items
    ]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def discover_repos(source: GitSource, token: str) -> list[DiscoveredRepo]:
    """
    Call the correct forge API for *source* and return all discovered repos.

    Raises ``ValueError`` for unsupported forge types.
    Raises ``httpx.HTTPStatusError`` for non-2xx forge API responses.
    """
    dispatch = {
        "github": _discover_github,
        "gitlab": _discover_gitlab,
        "azure_devops": _discover_azure_devops,
        "bitbucket": _discover_bitbucket,
    }
    fn = dispatch.get(source.forge_type)
    if fn is None:
        raise ValueError(
            f"Discovery is not supported for forge_type {source.forge_type!r}. "
            f"Supported: {sorted(dispatch)}"
        )
    return await fn(source, token)


# ---------------------------------------------------------------------------
# Lightweight connection test
# ---------------------------------------------------------------------------


async def test_connection(source: GitSource, token: str) -> str:
    """
    Make a single authenticated API call per forge type to verify credentials.

    Returns a short message such as "Authenticated as <login>".
    Raises ``httpx.HTTPStatusError`` on non-2xx responses, or any other
    ``httpx`` exception for network / TLS failures.
    """
    base = (source.base_url or "").rstrip("/")
    forge = source.forge_type

    async with httpx.AsyncClient(verify=source.verify_ssl, timeout=10.0) as client:
        if forge == "github":
            base = base or "https://api.github.com"
            r = await client.get(
                f"{base}/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            r.raise_for_status()
            login = r.json().get("login", "?")
            return f"Authenticated as {login}"

        elif forge == "gitlab":
            base = base or "https://gitlab.com"
            r = await client.get(
                f"{base}/api/v4/user",
                headers={"PRIVATE-TOKEN": token},
            )
            r.raise_for_status()
            username = r.json().get("username", "?")
            return f"Authenticated as {username}"

        elif forge == "azure_devops":
            base = base or "https://dev.azure.com"
            cred = base64.b64encode(f":{token}".encode()).decode()
            r = await client.get(
                f"{base}/_apis/projects?$top=1&api-version=7.1",
                headers={"Authorization": f"Basic {cred}"},
            )
            r.raise_for_status()
            return "Azure DevOps connection successful"

        elif forge == "bitbucket":
            base = base or "https://api.bitbucket.org/2.0"
            r = await client.get(
                f"{base}/user",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            account_id = r.json().get("account_id", "?")
            return f"Authenticated (account_id: {account_id})"

        else:
            # Generic: validate that a GET on base_url succeeds
            r = await client.get(base, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            return "Connection successful"


# ---------------------------------------------------------------------------
# Database upsert
# ---------------------------------------------------------------------------


def upsert_repos(
    source_id: UUID,
    discovered: list[DiscoveredRepo],
    session: Session,
) -> tuple[int, int]:
    """
    Atomically upsert discovered repos using SQLite ON CONFLICT DO UPDATE.

    Returns ``(new_count, updated_count)``.

    Uses a database-level upsert to avoid race conditions and any
    ORM-level type-coercion issues with the WHERE clause comparison.
    """
    if not discovered:
        return 0, 0

    now = datetime.now(UTC)

    # Snapshot existing names before the upsert (used only for counting).
    # Even if this SELECT returns an incomplete set, the upsert itself is
    # always correct due to ON CONFLICT DO UPDATE.
    existing_names: set[str] = {
        r.repo_name
        for r in session.exec(select(Repository).where(Repository.source_id == source_id)).all()
    }

    # source_id stored as hex (32 chars, no hyphens) — explicit to match
    # what _UUIDString.process_bind_param produces for the column.
    source_id_hex = source_id.hex

    values = [
        {
            "id": uuid4(),  # UUID object; SQLAlchemy type handles conversion
            "source_id": source_id_hex,  # Explicit hex to match _UUIDString storage
            "repo_name": d.name,
            "clone_url": d.clone_url,
            "default_branch": d.default_branch or "main",
            "description": d.description,
            "is_archived": d.is_archived,
            "last_seen_at": now,
            "created_at": now,  # Ignored by ON CONFLICT update path
        }
        for d in discovered
    ]

    stmt = sqlite_insert(Repository).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source_id", "repo_name"],
        set_={
            "clone_url": stmt.excluded.clone_url,
            "default_branch": stmt.excluded.default_branch,
            "description": stmt.excluded.description,
            "is_archived": stmt.excluded.is_archived,
            "last_seen_at": stmt.excluded.last_seen_at,
        },
    )
    session.execute(stmt)
    session.commit()

    new_count = sum(1 for d in discovered if d.name not in existing_names)
    updated_count = len(discovered) - new_count
    logger.info(
        "Discovery for source %s: %d new repo(s), %d updated",
        source_id,
        new_count,
        updated_count,
    )
    return new_count, updated_count


# ---------------------------------------------------------------------------
# Top-level orchestrator (called by API endpoint and scheduler)
# ---------------------------------------------------------------------------


async def run_discovery(
    source_id: UUID,
    engine: Any,
    fernet_key: bytes,
) -> dict[str, int]:
    """
    Full discovery flow: load source from DB, decrypt token, call forge
    API, upsert results.

    Returns ``{"new": n, "updated": n, "total": n}``.
    """
    from cryptography.fernet import Fernet

    fernet = Fernet(fernet_key)

    # Phase 1: load source — short-lived session so it's closed before HTTP calls
    with Session(engine) as load_session:
        source = load_session.get(GitSource, source_id)
        if source is None:
            raise ValueError(f"Source {source_id!r} not found")
        token = fernet.decrypt(source.auth_credential).decode()
        # Expunge so we can use the object after the session closes
        load_session.expunge(source)

    # Phase 2: async HTTP — no DB session held open
    discovered = await discover_repos(source, token)

    # Phase 3: upsert in a fresh session
    with Session(engine) as upsert_session:
        new_count, updated_count = upsert_repos(source_id, discovered, upsert_session)

    return {"new": new_count, "updated": updated_count, "total": len(discovered)}
