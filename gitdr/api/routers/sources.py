"""API routes for Git sources (forges / VCS hosts)."""

import logging
from datetime import UTC, datetime
from uuid import UUID

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, col, select

from gitdr.api.deps import get_fernet, get_session
from gitdr.api.schemas import (
    ConnectionTestRequest,
    ConnectionTestResponse,
    GitSourceCreate,
    GitSourceRead,
    GitSourceUpdate,
    RepositoryRead,
)
from gitdr.database.models import GitSource, Repository
from gitdr.services.discovery import discover_repos, test_connection, upsert_repos

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("/", response_model=list[GitSourceRead])
def list_sources(session: Session = Depends(get_session)) -> list[GitSource]:
    return list(session.exec(select(GitSource).order_by(col(GitSource.created_at).desc())).all())


@router.post("/test-connection", response_model=ConnectionTestResponse)
async def test_connection_probe(
    data: ConnectionTestRequest,
) -> ConnectionTestResponse:
    """Test credentials before saving a new source — nothing is written to the DB."""
    from uuid import uuid4

    temp = GitSource(
        id=uuid4(),
        name="probe",
        forge_type=data.forge_type,
        base_url=data.base_url,
        auth_type="pat",
        auth_credential=b"",
        org_or_group=data.org_or_group,
        verify_ssl=data.verify_ssl,
    )
    try:
        msg = await test_connection(temp, data.auth_credential)
        return ConnectionTestResponse(ok=True, message=msg)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Authentication failed ({exc.response.status_code}): {exc.response.text[:200]}",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/", response_model=GitSourceRead, status_code=status.HTTP_201_CREATED)
def create_source(
    data: GitSourceCreate,
    session: Session = Depends(get_session),
    fernet: Fernet = Depends(get_fernet),
) -> GitSource:
    encrypted = fernet.encrypt(data.auth_credential.encode())
    source = GitSource(
        name=data.name,
        forge_type=data.forge_type,
        base_url=data.base_url,
        auth_type=data.auth_type,
        auth_credential=encrypted,
        org_or_group=data.org_or_group,
        verify_ssl=data.verify_ssl,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@router.get("/{source_id}", response_model=GitSourceRead)
def get_source(source_id: UUID, session: Session = Depends(get_session)) -> GitSource:
    source = session.get(GitSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@router.put("/{source_id}", response_model=GitSourceRead)
def update_source(
    source_id: UUID,
    data: GitSourceUpdate,
    session: Session = Depends(get_session),
    fernet: Fernet = Depends(get_fernet),
) -> GitSource:
    source = session.get(GitSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    updates = data.model_dump(exclude_unset=True)
    if "auth_credential" in updates:
        updates["auth_credential"] = fernet.encrypt(updates["auth_credential"].encode())

    for key, value in updates.items():
        setattr(source, key, value)
    source.updated_at = datetime.now(UTC)

    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: UUID, session: Session = Depends(get_session)) -> None:
    source = session.get(GitSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    session.delete(source)
    session.commit()


@router.post("/{source_id}/test-connection", response_model=ConnectionTestResponse)
async def test_connection_existing(
    source_id: UUID,
    session: Session = Depends(get_session),
    fernet: Fernet = Depends(get_fernet),
) -> ConnectionTestResponse:
    """Test connection for an already-saved source using its stored credentials."""
    source = session.get(GitSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    token = fernet.decrypt(source.auth_credential).decode()
    try:
        msg = await test_connection(source, token)
        return ConnectionTestResponse(ok=True, message=msg)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Authentication failed ({exc.response.status_code})",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{source_id}/discover", response_model=list[RepositoryRead])
async def trigger_discovery(
    source_id: UUID,
    session: Session = Depends(get_session),
    fernet: Fernet = Depends(get_fernet),
) -> list[Repository]:
    """Discover repositories synchronously and return the updated list."""
    source = session.get(GitSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    token = fernet.decrypt(source.auth_credential).decode()
    try:
        discovered = await discover_repos(source, token)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"Forge API error: {exc.response.status_code}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    upsert_repos(source_id, discovered, session)
    return list(
        session.exec(
            select(Repository)
            .where(Repository.source_id == source_id)
            .order_by(Repository.repo_name)
        ).all()
    )


@router.get("/{source_id}/repositories", response_model=list[RepositoryRead])
def list_source_repositories(
    source_id: UUID, session: Session = Depends(get_session)
) -> list[Repository]:
    source = session.get(GitSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return list(
        session.exec(
            select(Repository)
            .where(Repository.source_id == source_id)
            .order_by(Repository.repo_name)
        ).all()
    )
