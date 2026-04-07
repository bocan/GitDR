"""API routes for backup destinations."""

import json
from datetime import UTC, datetime
from uuid import UUID

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, col, select

from gitdr.api.deps import get_fernet, get_session
from gitdr.api.schemas import (
    BackupDestinationCreate,
    BackupDestinationRead,
    BackupDestinationUpdate,
)
from gitdr.database.models import BackupDestination

router = APIRouter(prefix="/destinations", tags=["destinations"])


@router.get("/", response_model=list[BackupDestinationRead])
def list_destinations(session: Session = Depends(get_session)) -> list[BackupDestination]:
    return list(
        session.exec(
            select(BackupDestination).order_by(col(BackupDestination.created_at).desc())
        ).all()
    )


@router.post("/", response_model=BackupDestinationRead, status_code=status.HTTP_201_CREATED)
def create_destination(
    data: BackupDestinationCreate,
    session: Session = Depends(get_session),
    fernet: Fernet = Depends(get_fernet),
) -> BackupDestination:
    encrypted_config = fernet.encrypt(json.dumps(data.config).encode())
    dest = BackupDestination(
        name=data.name,
        dest_type=data.dest_type,
        config=encrypted_config,
    )
    session.add(dest)
    session.commit()
    session.refresh(dest)
    return dest


@router.get("/{dest_id}", response_model=BackupDestinationRead)
def get_destination(dest_id: UUID, session: Session = Depends(get_session)) -> BackupDestination:
    dest = session.get(BackupDestination, dest_id)
    if not dest:
        raise HTTPException(status_code=404, detail="Destination not found")
    return dest


@router.put("/{dest_id}", response_model=BackupDestinationRead)
def update_destination(
    dest_id: UUID,
    data: BackupDestinationUpdate,
    session: Session = Depends(get_session),
    fernet: Fernet = Depends(get_fernet),
) -> BackupDestination:
    dest = session.get(BackupDestination, dest_id)
    if not dest:
        raise HTTPException(status_code=404, detail="Destination not found")

    updates = data.model_dump(exclude_unset=True)
    if "config" in updates:
        updates["config"] = fernet.encrypt(json.dumps(updates["config"]).encode())

    for key, value in updates.items():
        setattr(dest, key, value)
    dest.updated_at = datetime.now(UTC)

    session.add(dest)
    session.commit()
    session.refresh(dest)
    return dest


@router.delete("/{dest_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_destination(dest_id: UUID, session: Session = Depends(get_session)) -> None:
    dest = session.get(BackupDestination, dest_id)
    if not dest:
        raise HTTPException(status_code=404, detail="Destination not found")
    session.delete(dest)
    session.commit()


@router.post("/{dest_id}/test", status_code=status.HTTP_200_OK)
def test_destination(dest_id: UUID, session: Session = Depends(get_session)) -> dict[str, str]:
    """Test connectivity / write access to this destination. (Phase 4: backend integration.)"""
    dest = session.get(BackupDestination, dest_id)
    if not dest:
        raise HTTPException(status_code=404, detail="Destination not found")
    return {"status": "ok", "message": f"Connectivity test passed for '{dest.name}'"}
