"""Centralized API authorization helpers."""

from __future__ import annotations

import os
from collections.abc import Iterable

from fastapi import HTTPException, Request

from nexus.api.deps import get_auth_store
from nexus.auth.store import AuthError
from nexus.registry import Registry

PRODUCT_ROLES = {"owner", "editor", "viewer"}
WRITE_ROLES = {"owner", "editor"}


def auth_mode() -> str:
    return (os.getenv("NEXUS_AUTH_MODE") or "").strip().lower()


def prod_enabled() -> bool:
    return (os.getenv("NEXUS_ENV") or "").strip().lower() == "production"


def auth_enabled() -> bool:
    return auth_mode() == "auth0" or bool(os.getenv("NEXUS_SECRET_KEY"))


def local_fs_enabled() -> bool:
    raw = os.getenv("NEXUS_ENABLE_LOCAL_FS_SOURCES")
    if raw is None:
        return not prod_enabled()
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user:
        return user
    if not auth_enabled():
        return {
            "id": "dev-admin",
            "email": "dev-admin@nexus.local",
            "name": "Dev Admin",
            "role": "admin",
            "status": "approved",
        }
    raise HTTPException(status_code=401, detail="authentication required")


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user.get("status") != "approved":
        raise HTTPException(status_code=403, detail="access request pending")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin required")
    return user


def public_user(user: dict, registry: Registry | None = None) -> dict:
    out = {k: v for k, v in user.items() if k not in {"password_hash"}}
    out.setdefault("name", out.get("name") or out.get("email", ""))
    if registry and out.get("id") and out.get("role") != "admin":
        out["products"] = registry.list_product_ids_for_user(str(out["id"]))
    else:
        out.setdefault("products", [])
    return out


def product_permissions(user: dict, product_role: str | None = None) -> dict:
    org_admin = user.get("role") == "admin"
    can_write = org_admin or product_role in WRITE_ROLES
    can_read = can_write or product_role == "viewer"
    return {
        "canManageSources": can_write,
        "canRunCouncil": can_write,
        "canOnboard": user.get("status") == "approved",
        "canReadProduct": can_read,
        "isOrgAdmin": org_admin,
        "settingsReadOnly": not org_admin,
    }


def assert_product_access(
    request: Request, registry: Registry, product_id: str, *, action: str = "read"
) -> dict:
    if not auth_enabled():
        return current_user(request)
    if not registry.get_product(product_id):
        raise HTTPException(status_code=404, detail="product not found")
    user = require_user(request)
    if user.get("role") == "admin":
        return user
    role = registry.get_product_role(product_id, str(user["id"]))
    if action == "read" and role in PRODUCT_ROLES:
        return user
    if action in {"manage", "source", "council", "approve"} and role in WRITE_ROLES:
        return user
    raise HTTPException(status_code=403, detail="product access denied")


def filter_products_for_user(request: Request, registry: Registry, products: list[dict]) -> list[dict]:
    user = require_user(request)
    if user.get("role") == "admin":
        return products
    allowed = set(registry.list_product_ids_for_user(str(user["id"])))
    return [p for p in products if p.get("id") in allowed]


def rate_limit(request: Request, *, bucket: str, limit: int, window_s: int) -> None:
    if not auth_enabled():
        return
    user = getattr(request.state, "user", None) or {}
    client_host = request.client.host if request.client else "unknown"
    subject = str(user.get("id") or user.get("email") or client_host)
    try:
        get_auth_store().check_rate_limit(
            bucket=bucket, subject=subject, limit=limit, window_s=window_s
        )
    except AuthError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e


def assert_any_product_access(
    request: Request, registry: Registry, product_ids: Iterable[str], *, action: str = "read"
) -> None:
    for product_id in product_ids:
        assert_product_access(request, registry, product_id, action=action)
