# routes_auth.py — /api/auth/* login, password change, admin user CRUD

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from backend.core.auth import (
    count_active_admins,
    create_access_token,
    hash_password,
    require_admin,
    require_user,
    user_to_public_dict,
    verify_password,
)
from backend.database import get_db
from backend.models import AppUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)

    @field_validator("new_password")
    @classmethod
    def _min_len(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("new_password must be at least 8 characters")
        return v


class CreateUserRequest(BaseModel):
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8)
    role: str = Field(default="user")

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        role = v.strip().lower()
        if role not in {"admin", "user"}:
            raise ValueError("role must be 'admin' or 'user'")
        return role

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.strip().lower()


class PatchUserRequest(BaseModel):
    email: str | None = None
    role: str | None = None
    is_active: bool | None = None
    new_password: str | None = Field(default=None, min_length=8)

    @field_validator("role")
    @classmethod
    def _role(cls, v: str | None) -> str | None:
        if v is None:
            return None
        role = v.strip().lower()
        if role not in {"admin", "user"}:
            raise ValueError("role must be 'admin' or 'user'")
        return role

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip().lower()


def _guard_admin_self_and_last(
    *,
    actor: AppUser,
    target: AppUser,
    new_role: str | None,
    new_is_active: bool | None,
    deleting: bool,
    db: Session,
) -> None:
    """
    Block self-delete / self-demote / self-deactivate, and protect the last
    remaining active admin. Raises HTTP 409 with a clear message.
    """
    demoting = new_role is not None and new_role != "admin" and target.role == "admin"
    deactivating = (
        new_is_active is False and target.is_active and target.role == "admin"
    )
    would_remove_admin = deleting or demoting or deactivating

    if actor.id == target.id and (deleting or demoting or deactivating):
        raise HTTPException(
            status_code=409,
            detail="You cannot delete, demote, or deactivate your own admin account.",
        )

    if would_remove_admin and target.role == "admin" and target.is_active:
        if count_active_admins(db) <= 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot delete or demote the last remaining active admin. "
                    "Promote another admin first."
                ),
            )


@router.post("/login")
async def login(
    payload: LoginRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Authenticate with email/password and return a JWT (never log credentials)."""
    try:
        email = payload.email.strip().lower()
        user = db.query(AppUser).filter(AppUser.email == email).first()
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid email or password.")
        if not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        token = create_access_token(
            user_id=user.id, email=user.email, role=user.role
        )
        return {
            "success": True,
            "data": {
                "token": token,
                "email": user.email,
                "role": user.role,
                "must_change_password": bool(user.must_change_password),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.critical("Unexpected login error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@router.post("/logout")
async def logout(_user: AppUser = Depends(require_user)) -> dict[str, Any]:
    """Client clears the token; server acknowledges logout."""
    return {"success": True, "data": {"message": "Logged out"}}


@router.get("/me")
async def me(user: AppUser = Depends(require_user)) -> dict[str, Any]:
    """Return the current authenticated user (no password_hash)."""
    return {"success": True, "data": user_to_public_dict(user)}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    user: AppUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Change password; clears must_change_password on success."""
    try:
        if not verify_password(payload.current_password, user.password_hash):
            raise HTTPException(
                status_code=401, detail="Current password is incorrect."
            )
        if len(payload.new_password) < 8:
            raise HTTPException(
                status_code=422,
                detail="new_password must be at least 8 characters",
            )
        user.password_hash = hash_password(payload.new_password)
        user.must_change_password = False
        db.commit()
        db.refresh(user)
        return {
            "success": True,
            "data": user_to_public_dict(user),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.critical("Unexpected change-password error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@router.get("/users")
async def list_users(
    _admin: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Admin: list all users (never return password_hash)."""
    users = db.query(AppUser).order_by(AppUser.id.asc()).all()
    return {
        "success": True,
        "data": [user_to_public_dict(u) for u in users],
    }


@router.post("/users")
async def create_user(
    payload: CreateUserRequest,
    _admin: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Admin: create a user."""
    try:
        existing = (
            db.query(AppUser).filter(AppUser.email == payload.email).first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409, detail="A user with that email already exists."
            )
        user = AppUser(
            email=payload.email,
            password_hash=hash_password(payload.password),
            role=payload.role,
            must_change_password=True,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return {"success": True, "data": user_to_public_dict(user)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.critical("Unexpected create-user error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: int,
    payload: PatchUserRequest,
    admin: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Admin: update email / role / active / reset password."""
    try:
        target = db.query(AppUser).filter(AppUser.id == user_id).first()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found.")

        _guard_admin_self_and_last(
            actor=admin,
            target=target,
            new_role=payload.role,
            new_is_active=payload.is_active,
            deleting=False,
            db=db,
        )

        if payload.email is not None and payload.email != target.email:
            clash = (
                db.query(AppUser)
                .filter(AppUser.email == payload.email, AppUser.id != target.id)
                .first()
            )
            if clash is not None:
                raise HTTPException(
                    status_code=409, detail="A user with that email already exists."
                )
            target.email = payload.email

        if payload.role is not None:
            target.role = payload.role

        if payload.is_active is not None:
            target.is_active = payload.is_active

        if payload.new_password is not None:
            target.password_hash = hash_password(payload.new_password)
            target.must_change_password = True

        db.commit()
        db.refresh(target)
        return {"success": True, "data": user_to_public_dict(target)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.critical("Unexpected patch-user error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Admin: delete a user (guards: self + last admin)."""
    try:
        target = db.query(AppUser).filter(AppUser.id == user_id).first()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found.")

        _guard_admin_self_and_last(
            actor=admin,
            target=target,
            new_role=None,
            new_is_active=None,
            deleting=True,
            db=db,
        )

        db.delete(target)
        db.commit()
        return {"success": True, "data": {"id": user_id, "deleted": True}}
    except HTTPException:
        raise
    except Exception as exc:
        logger.critical("Unexpected delete-user error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from exc
