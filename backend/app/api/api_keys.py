"""API-key management endpoints.

Lets researchers create long-lived keys for external apps that want to read
live cage data programmatically (Grafana, Jupyter notebooks, custom dashboards).

Security:
- Plaintext key is shown ONCE on creation; the DB only stores SHA-256(key).
- Revocation is a soft delete (revoked_at timestamp) so we keep an audit trail.
- Keys are scoped to the issuing user — they get the same role/permissions.
"""
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ApiKey, User
from ..security import generate_api_key, get_current_user

router = APIRouter(prefix="/api/v1/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    label: str = Field(default="", max_length=128, description="Human-friendly name")
    expires_in_days: Optional[int] = Field(
        default=None, ge=1, le=3650, description="Optional TTL; null = never expires"
    )


class ApiKeyOut(BaseModel):
    id: int
    label: str
    prefix: str
    created_at: datetime
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]
    revoked: bool


class ApiKeyCreated(ApiKeyOut):
    key: str  # plaintext — only returned at creation time


def _to_out(k: ApiKey) -> dict:
    return {
        "id": k.id,
        "label": k.label,
        "prefix": k.prefix,
        "created_at": k.created_at,
        "last_used_at": k.last_used_at,
        "expires_at": k.expires_at,
        "revoked": k.revoked_at is not None,
    }


@router.get("", response_model=list[ApiKeyOut])
def list_keys(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    keys = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user.id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )
    return [_to_out(k) for k in keys]


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
def create_key(
    body: ApiKeyCreate,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    plaintext, prefix, key_hash = generate_api_key()
    expires_at = None
    if body.expires_in_days:
        expires_at = datetime.now(tz=timezone.utc) + timedelta(days=body.expires_in_days)

    rec = ApiKey(
        user_id=user.id,
        label=body.label or f"key-{prefix[4:]}",
        prefix=prefix,
        key_hash=key_hash,
        expires_at=expires_at,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)

    out = _to_out(rec)
    out["key"] = plaintext  # plaintext shown ONCE
    return out


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_key(
    key_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    rec = (
        db.query(ApiKey)
        .filter(ApiKey.id == key_id, ApiKey.user_id == user.id)
        .one_or_none()
    )
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Unknown API key."},
        )
    if rec.revoked_at is None:
        rec.revoked_at = datetime.now(tz=timezone.utc)
        db.commit()
    return None
