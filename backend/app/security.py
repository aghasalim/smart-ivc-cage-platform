import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import ApiKey, User

settings = get_settings()

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

# ── API-key helpers ──────────────────────────────────────────────────────────
_API_KEY_PREFIX = "ivc_"  # human-recognisable so users don't paste random tokens


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext_key, prefix_first_12_chars, sha256_hash).

    Format:  ivc_<43 chars of url-safe base64>
    The plaintext is shown to the user ONCE; the DB only stores the hash.
    """
    raw = secrets.token_urlsafe(32)
    key = f"{_API_KEY_PREFIX}{raw}"
    prefix = key[:12]  # ivc_ + 8 visible chars
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    return key, prefix, key_hash


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def create_token(*, subject: str, role: str, expires_minutes: int | None = None) -> str:
    expires = datetime.now(tz=timezone.utc) + timedelta(
        minutes=expires_minutes or settings.jwt_expire_minutes
    )
    payload = {"sub": subject, "role": role, "exp": expires}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": str(exc)},
        ) from exc


def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    """Authenticate via JWT (preferred) or X-API-Key header.

    External apps that integrate with the platform use the long-lived API key
    so they don't need to log in / refresh tokens. Internal browser clients use
    the short-lived JWT.
    """
    # 1. JWT bearer token path (primary, used by the SPA)
    if token:
        data = decode_token(token)
        user = db.query(User).filter(User.username == data["sub"]).one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_TOKEN", "message": "Unknown subject."},
            )
        return user

    # 2. API-key path (for programmatic access / external apps)
    if x_api_key:
        now = datetime.now(tz=timezone.utc)
        api_key = (
            db.query(ApiKey)
            .filter(ApiKey.key_hash == hash_api_key(x_api_key))
            .one_or_none()
        )
        if not api_key or api_key.revoked_at is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_API_KEY", "message": "Invalid or revoked API key."},
            )
        if api_key.expires_at is not None and api_key.expires_at < now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "EXPIRED_API_KEY", "message": "API key expired."},
            )
        user = db.query(User).filter(User.id == api_key.user_id).one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_API_KEY", "message": "API key user removed."},
            )
        # Update last_used (best-effort, don't fail auth if commit errors)
        try:
            api_key.last_used_at = now
            db.commit()
        except Exception:
            db.rollback()
        return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "INVALID_TOKEN", "message": "Missing token or API key."},
    )


def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "Admin role required."},
        )
    return user
