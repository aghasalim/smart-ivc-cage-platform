import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..limiter import limiter
from ..models import User
from ..schemas import LoginIn, TokenOut, UserOut
from ..security import create_token, get_current_user, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

settings = get_settings()
log = logging.getLogger(__name__)

# Pre-hashed dummy password used for timing-attack mitigation when the
# username doesn't exist. Computed once at module import.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


@router.post("/login", response_model=TokenOut)
@limiter.limit(settings.rate_limit_login)
def login(payload: LoginIn, request: Request, db: Annotated[Session, Depends(get_db)]) -> TokenOut:
    user = db.query(User).filter(User.username == payload.username).one_or_none()

    # Constant-time path: always run a hash verification (even on missing user)
    # so the response time can't reveal whether a username exists.
    if user is None:
        verify_password(payload.password, _DUMMY_HASH)
        ok = False
    else:
        ok = verify_password(payload.password, user.password_hash)

    if not ok or user is None:
        log.info({
            "event": "auth.failed",
            "username": payload.username,
            "ip": request.client.host if request.client else None,
        })
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CREDENTIALS", "message": "Invalid username or password."},
        )

    token = create_token(subject=user.username, role=user.role)
    log.info({"event": "auth.success", "username": user.username, "role": user.role})
    return TokenOut(access_token=token, expires_in=settings.jwt_expire_minutes * 60)


@router.post("/refresh", response_model=TokenOut)
def refresh(user: Annotated[User, Depends(get_current_user)]) -> TokenOut:
    token = create_token(subject=user.username, role=user.role)
    return TokenOut(access_token=token, expires_in=settings.jwt_expire_minutes * 60)


@router.get("/me", response_model=UserOut)
def me(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user
