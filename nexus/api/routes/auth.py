"""Authentication and access-request routes."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from nexus.api.authz import (
    auth_mode,
    current_user,
    product_permissions,
    public_user,
    require_admin,
)
from nexus.api.deps import get_auth_store, get_registry
from nexus.auth.store import CSRF_COOKIE, SESSION_COOKIE, SESSION_TTL_DAYS, AuthError, AuthStore
from nexus.registry import Registry

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str = Field(..., min_length=1)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _valid_email(value)


class AccessRequestBody(BaseModel):
    email: str | None = None
    name: str = ""
    reason: str = ""

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _valid_email(value)


class DecideAccessBody(BaseModel):
    role: str = "viewer"
    password: str | None = Field(None, min_length=12)


@router.post("/login")
async def login(
    request: Request,
    body: LoginRequest,
    response: Response,
    store: AuthStore = Depends(get_auth_store),
) -> dict:
    if auth_mode() == "auth0":
        raise HTTPException(status_code=404, detail="local login disabled")
    try:
        store.check_rate_limit(
            bucket="login",
            subject=_client_key(request, "login"),
            limit=8,
            window_s=300,
        )
    except AuthError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    try:
        result = store.login(email=str(body.email), password=body.password)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    expires = datetime.fromisoformat(result.expires_at)
    response.set_cookie(
        SESSION_COOKIE,
        result.session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        expires=expires,
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
    )
    response.set_cookie(
        CSRF_COOKIE,
        result.csrf_token,
        httponly=False,
        secure=True,
        samesite="lax",
        expires=expires,
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
    )
    return {"user": _public_user(result.user), "csrf_token": result.csrf_token}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    store: AuthStore = Depends(get_auth_store),
) -> dict:
    store.revoke_session(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {"ok": True}


@router.get("/me")
async def me(
    request: Request,
    registry: Registry = Depends(get_registry),
) -> dict:
    user = current_user(request)
    role_by_product = registry.list_product_memberships(user["id"]) if user.get("id") else {}
    current_role = next(iter(role_by_product.values()), None)
    return {
        "user": public_user(user, registry),
        "permissions": product_permissions(user, current_role),
        "memberships": role_by_product,
    }


@router.post("/request-access")
async def request_access(
    request: Request,
    body: AccessRequestBody,
    store: AuthStore = Depends(get_auth_store),
) -> dict:
    requester = getattr(request.state, "user", None)
    if requester is None and not body.email:
        raise HTTPException(status_code=422, detail="email is required")
    try:
        store.check_rate_limit(
            bucket="access_request",
            subject=_client_key(request, "access"),
            limit=5,
            window_s=3600,
        )
    except AuthError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    email = requester.get("email") if requester else str(body.email)
    name = requester.get("name") if requester else body.name
    req = store.request_access(
        email=str(email), name=str(name or ""), reason=body.reason
    )
    return {"ok": True, "request": req}


@router.get("/access-requests")
async def list_access_requests(
    request: Request,
    status: str | None = "pending",
    store: AuthStore = Depends(get_auth_store),
) -> dict:
    require_admin(request)
    return {"requests": store.list_access_requests(status=status)}


@router.post("/access-requests/{request_id}/approve")
async def approve_access_request(
    request_id: str,
    request: Request,
    body: DecideAccessBody = Body(default_factory=DecideAccessBody),
    store: AuthStore = Depends(get_auth_store),
) -> dict:
    admin = require_admin(request)
    try:
        req = store.decide_access_request(
            request_id,
            status="approved",
            decided_by=admin["email"],
            password=body.password,
            role=body.role,
            require_password=auth_mode() != "auth0",
        )
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "request": req}


@router.post("/access-requests/{request_id}/reject")
async def reject_access_request(
    request_id: str,
    request: Request,
    store: AuthStore = Depends(get_auth_store),
) -> dict:
    admin = require_admin(request)
    try:
        req = store.decide_access_request(
            request_id, status="rejected", decided_by=admin["email"]
        )
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "request": req}


@router.get("/users")
async def list_users(
    request: Request,
    store: AuthStore = Depends(get_auth_store),
) -> dict:
    require_admin(request)
    return {"users": [_public_user(u) for u in store.list_users()]}


@router.post("/users/{email}/revoke")
async def revoke_user(
    email: str,
    request: Request,
    store: AuthStore = Depends(get_auth_store),
) -> dict:
    require_admin(request)
    try:
        user = store.revoke_user(email)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "user": _public_user(user)}


def _public_user(user: dict) -> dict:
    return public_user(user)


def _valid_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("valid email required")
    return email


def _client_key(request: Request, bucket: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{bucket}:{host}"
