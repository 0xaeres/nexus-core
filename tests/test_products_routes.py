"""Route-level tests for product creation."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import nexus.api.app as api_app
from nexus.api.app import app
from nexus.api.deps import get_auth_store, get_registry
from nexus.auth.store import AuthStore
from nexus.registry import Registry


def test_create_product_accepts_owner_team(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.db")
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        client = TestClient(app)
        r = client.post(
            "/products",
            json={
                "id": "payments-api",
                "name": "Payments API",
                "owner": {"team": "Payments Platform"},
            },
        )
    finally:
        app.dependency_overrides.pop(get_registry, None)

    assert r.status_code == 200
    body = r.json()
    assert body["owner"] == {"team": "Payments Platform"}


def test_me_requires_auth_when_auth_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    auth_store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    registry = Registry(tmp_path / "registry.db")
    monkeypatch.setattr(api_app, "get_auth_store", lambda: auth_store)
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        client = TestClient(app)
        r = client.get("/me")
    finally:
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_registry, None)

    assert r.status_code == 401
