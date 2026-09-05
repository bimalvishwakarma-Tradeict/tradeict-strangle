# auth.py — bcrypt password hashing, JWT, FastAPI auth dependencies, user seed

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.config import AUTH_SECRET_KEY
from backend.database import SessionLocal, get_db
from backend.models import AppUser

logger = logging.getLogger(__name__)

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 12
_SEED_PASSWORD = "123456789"
# bcrypt input limit — never truncate; reject at API boundary instead
BCRYPT_MAX_PASSWORD_BYTES = 72

_SEED_USERS: tuple[tuple[str, str], ...] = (
    ("bimal.vishwakarma@gmail.com", "admin"),
    ("jai.kvspl@gmail.com", "user"),
    ("anshul@tradeictearner.online", "user"),
)

_bearer = HTTPBearer(auto_error=False)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def get_auth_secret() -> str:
    """Return AUTH_SECRET_KEY or raise — never fall back to a hardcoded default."""
    secret = (AUTH_SECRET_KEY or "").strip()
    if not secret:
        raise RuntimeError(
            "AUTH_SECRET_KEY is missing or empty. Set it in .env before starting "
            "the server. Refusing to start without a JWT signing secret."
        )
    return secret


def password_byte_length(plain: str) -> int:
    """UTF-8 byte length of a password (bcrypt limit is 72 bytes)."""
    return len(plain.encode("utf-8"))


def hash_password(plain: str) -> str:
    """Hash a plain password with bcrypt. Never log the plain value."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify plain password against a stored bcrypt hash ($2b$ compatible)."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(*, user_id: int, email: str, role: str) -> str:
    """Issue an HS256 JWT valid for JWT_EXPIRY_HOURS."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, get_auth_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate JWT. Raises HTTPException 401 on failure."""
    try:
        return jwt.decode(
            token,
            get_auth_secret(),
            algorithms=[JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=401, detail="Token expired. Please log in again."
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=401, detail="Invalid authentication token."
        ) from exc


def user_to_public_dict(user: AppUser) -> dict[str, Any]:
    """Serialize AppUser for API responses (never includes password_hash)."""
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "must_change_password": bool(user.must_change_password),
        "is_active": bool(user.is_active),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _load_user_from_token(token: str, db: Session) -> AppUser:
    payload = decode_access_token(token)
    raw_sub = payload.get("sub")
    try:
        user_id = int(raw_sub)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Invalid authentication token."
        ) from exc
    user = db.query(AppUser).filter(AppUser.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=401, detail="User not found or inactive."
        )
    return user


async def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AppUser:
    """FastAPI dependency: require a valid Bearer JWT for an active user."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401, detail="Authentication required."
        )
    return _load_user_from_token(credentials.credentials, db)


async def require_admin(
    user: AppUser = Depends(require_user),
) -> AppUser:
    """FastAPI dependency: require an authenticated admin user."""
    if user.role != "admin":
        raise HTTPException(
            status_code=403, detail="Admin privileges required."
        )
    return user


async def require_user_for_slave(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AppUser | None:
    """
    Slave router dependency.

    GET /api/slave/overview from loopback (127.0.0.1 / ::1) may proceed
    without a token (Earner botSyncService). All other slave routes require auth.
    """
    path = request.url.path.rstrip("/")
    is_overview = request.method == "GET" and path.endswith("/api/slave/overview")
    if is_overview:
        host = request.client.host if request.client else None
        if host in LOOPBACK_HOSTS:
            if credentials is None or not credentials.credentials:
                from backend.core.bot_logger import log_and_buffer

                log_and_buffer(
                    "AUTH_LOOPBACK_BYPASS",
                    0,
                    {
                        "path": path,
                        "host": host,
                        "summary": (
                            f"[AUTH_LOOPBACK_BYPASS] {path} from {host}"
                        ),
                    },
                )
                return None
            # Token present from loopback — still validate it
            return _load_user_from_token(credentials.credentials, db)

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401, detail="Authentication required."
        )
    return _load_user_from_token(credentials.credentials, db)


def authenticate_ws_token(token: str | None, db: Session) -> AppUser:
    """Validate JWT for WebSocket ?token= query param. Raises ValueError on fail."""
    if not token or not str(token).strip():
        raise ValueError("Missing authentication token")
    try:
        return _load_user_from_token(str(token).strip(), db)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc


def seed_users() -> None:
    """
    Create the three Phase-1 seeded users if absent.
    Idempotent — never resets an existing user's password.
    """
    get_auth_secret()  # fail fast if secret missing
    db = SessionLocal()
    try:
        created = 0
        for email, role in _SEED_USERS:
            existing = (
                db.query(AppUser)
                .filter(AppUser.email == email.lower().strip())
                .first()
            )
            if existing is not None:
                continue
            user = AppUser(
                email=email.lower().strip(),
                password_hash=hash_password(_SEED_PASSWORD),
                role=role,
                must_change_password=True,
                is_active=True,
            )
            db.add(user)
            created += 1
        if created:
            db.commit()
            logger.info("Auth seed: created %s user(s)", created)
        else:
            logger.info("Auth seed: all seeded users already present")
    finally:
        db.close()


def count_active_admins(db: Session) -> int:
    """Count active users with role=admin."""
    return (
        db.query(AppUser)
        .filter(AppUser.role == "admin", AppUser.is_active.is_(True))
        .count()
    )
