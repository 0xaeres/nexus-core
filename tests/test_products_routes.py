"""Route-level tests for product creation."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import nexus.api.app as api_app
from nexus.api.app import app
from nexus.api.deps import get_auth_store, get_config_dep, get_registry
from nexus.api.routes import products as products_route
from nexus.auth.store import AuthStore
from nexus.config import NexusConfig
from nexus.registry import Registry
from nexus.tools.delete_product import DeleteProductReport


def _config(tmp_path: Path) -> NexusConfig:
    return NexusConfig(
        models={
            "council": {"provider": "test", "model": "test"},
            "light": {"provider": "test", "model": "test"},
            "embedding": {"provider": "test", "model": "test", "url": "http://embed"},
            "reranker": {"provider": "test", "model": "test", "url": "http://rerank"},
        },
        storage={
            "proposal_queue": tmp_path / "queue.db",
            "council_checkpoint": tmp_path / "council.sqlite",
        },
    )


def _clear_bootstrap_env(monkeypatch) -> None:
    monkeypatch.delenv("NEXUS_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("NEXUS_BOOTSTRAP_ADMIN_PASSWORD", raising=False)


def test_create_product_accepts_owner_team(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NEXUS_SECRET_KEY", raising=False)
    _clear_bootstrap_env(monkeypatch)
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
    _clear_bootstrap_env(monkeypatch)
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


def test_owner_can_delete_product(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    _clear_bootstrap_env(monkeypatch)
    auth_store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    user = auth_store.create_user(
        email="owner@example.com",
        password="correct horse battery staple",
        role="viewer",
    )
    registry = Registry(tmp_path / "registry.db")
    registry.upsert_product(
        {"id": "demo", "name": "Demo", "tagline": "", "owner": {}, "onboardedAt": "now"}
    )
    registry.grant_product_role("demo", user["id"], "owner")
    cfg = _config(tmp_path)
    calls: list[str] = []

    async def fake_delete_product(**kwargs):
        calls.append(kwargs["product_id"])
        return DeleteProductReport(product_id=kwargs["product_id"], registry={"products": 1})

    monkeypatch.setattr(api_app, "get_auth_store", lambda: auth_store)
    monkeypatch.setattr(products_route, "delete_product", fake_delete_product)
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_config_dep] = lambda: cfg
    try:
        client = TestClient(app, base_url="https://testserver")
        login = client.post(
            "/auth/login",
            json={
                "email": "owner@example.com",
                "password": "correct horse battery staple",
            },
        )
        csrf = client.cookies.get("nexus_csrf")
        res = client.delete("/products/demo", headers={"X-Nexus-CSRF": csrf or ""})
    finally:
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_config_dep, None)

    assert login.status_code == 200
    assert res.status_code == 200
    assert res.json()["report"]["registry"] == {"products": 1}
    assert calls == ["demo"]


def test_viewer_cannot_delete_product(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    _clear_bootstrap_env(monkeypatch)
    auth_store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    user = auth_store.create_user(
        email="viewer@example.com",
        password="correct horse battery staple",
        role="viewer",
    )
    registry = Registry(tmp_path / "registry.db")
    registry.upsert_product(
        {"id": "demo", "name": "Demo", "tagline": "", "owner": {}, "onboardedAt": "now"}
    )
    registry.grant_product_role("demo", user["id"], "viewer")
    cfg = _config(tmp_path)
    called = False

    async def fake_delete_product(**_kwargs):
        nonlocal called
        called = True
        return DeleteProductReport(product_id="demo")

    monkeypatch.setattr(api_app, "get_auth_store", lambda: auth_store)
    monkeypatch.setattr(products_route, "delete_product", fake_delete_product)
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_config_dep] = lambda: cfg
    try:
        client = TestClient(app, base_url="https://testserver")
        client.post(
            "/auth/login",
            json={
                "email": "viewer@example.com",
                "password": "correct horse battery staple",
            },
        )
        csrf = client.cookies.get("nexus_csrf")
        res = client.delete("/products/demo", headers={"X-Nexus-CSRF": csrf or ""})
    finally:
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_config_dep, None)

    assert res.status_code == 403
    assert called is False


def test_delete_product_reports_dependency_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    _clear_bootstrap_env(monkeypatch)
    auth_store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    auth_store.create_user(
        email="admin@example.com",
        password="correct horse battery staple",
        role="admin",
    )
    registry = Registry(tmp_path / "registry.db")
    registry.upsert_product(
        {"id": "demo", "name": "Demo", "tagline": "", "owner": {}, "onboardedAt": "now"}
    )
    cfg = _config(tmp_path)

    async def fake_delete_product(**_kwargs):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(api_app, "get_auth_store", lambda: auth_store)
    monkeypatch.setattr(products_route, "delete_product", fake_delete_product)
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_config_dep] = lambda: cfg
    try:
        client = TestClient(app, base_url="https://testserver")
        client.post(
            "/auth/login",
            json={
                "email": "admin@example.com",
                "password": "correct horse battery staple",
            },
        )
        csrf = client.cookies.get("nexus_csrf")
        res = client.delete("/products/demo", headers={"X-Nexus-CSRF": csrf or ""})
    finally:
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_config_dep, None)

    assert res.status_code == 502
    assert res.json()["detail"] == "failed to purge product 'demo'"


def test_delete_product_returns_404_for_missing_product(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    _clear_bootstrap_env(monkeypatch)
    auth_store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    auth_store.create_user(
        email="admin@example.com",
        password="correct horse battery staple",
        role="admin",
    )
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    called = False

    async def fake_delete_product(**_kwargs):
        nonlocal called
        called = True
        return DeleteProductReport(product_id="missing")

    monkeypatch.setattr(api_app, "get_auth_store", lambda: auth_store)
    monkeypatch.setattr(products_route, "delete_product", fake_delete_product)
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_config_dep] = lambda: cfg
    try:
        client = TestClient(app, base_url="https://testserver")
        client.post(
            "/auth/login",
            json={
                "email": "admin@example.com",
                "password": "correct horse battery staple",
            },
        )
        csrf = client.cookies.get("nexus_csrf")
        res = client.delete("/products/missing", headers={"X-Nexus-CSRF": csrf or ""})
    finally:
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_config_dep, None)

    assert res.status_code == 404
    assert called is False
