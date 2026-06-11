from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import nexus.api.app as api_app
from nexus.api.app import app
from nexus.api.deps import get_auth_store, get_proposal_queue, get_registry, get_skill_store
from nexus.auth.store import CSRF_COOKIE, SESSION_COOKIE, AuthStore
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.skills.store import SkillStore


def test_login_sets_secure_session_and_csrf(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    store.create_user(
        email="owner@example.com",
        password="correct horse battery staple",
        role="admin",
    )
    monkeypatch.setattr(api_app, "get_auth_store", lambda: store)
    app.dependency_overrides[get_auth_store] = lambda: store
    try:
        client = TestClient(app, base_url="https://testserver")
        res = client.post(
            "/auth/login",
            json={
                "email": "owner@example.com",
                "password": "correct horse battery staple",
            },
        )
        assert res.status_code == 200
        assert client.cookies.get(SESSION_COOKIE)
        assert client.cookies.get(CSRF_COOKIE)

        csrf = client.cookies.get(CSRF_COOKIE)
        missing = client.post("/auth/logout", json={})
        assert missing.status_code == 403

        ok = client.post("/auth/logout", json={}, headers={"X-Nexus-CSRF": csrf})
        assert ok.status_code == 200
    finally:
        app.dependency_overrides.pop(get_auth_store, None)


def test_admin_api_key_allows_protected_route(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    monkeypatch.setenv("NEXUS_ADMIN_API_KEY", "admin-key")
    auth_store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    registry = Registry(tmp_path / "registry.db")
    queue = ProposalQueue(tmp_path / "queue.db")
    store = SkillStore(tmp_path / "skills")
    monkeypatch.setattr(api_app, "get_auth_store", lambda: auth_store)
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_proposal_queue] = lambda: queue
    app.dependency_overrides[get_skill_store] = lambda: store
    try:
        client = TestClient(app, base_url="https://testserver")

        unauth = client.get("/products")
        assert unauth.status_code == 401

        authed = client.get("/products", headers={"Authorization": "Bearer admin-key"})
    finally:
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_proposal_queue, None)
        app.dependency_overrides.pop(get_skill_store, None)

    assert authed.status_code == 200


def test_access_request_public_when_auth_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    monkeypatch.setattr(api_app, "get_auth_store", lambda: store)
    app.dependency_overrides[get_auth_store] = lambda: store
    try:
        client = TestClient(app, base_url="https://testserver")
        res = client.post(
            "/auth/request-access",
            json={"email": "dev@example.com", "name": "Dev"},
        )
        assert res.status_code == 200
        assert store.list_access_requests(status="pending")[0]["email"] == "dev@example.com"
    finally:
        app.dependency_overrides.pop(get_auth_store, None)


def test_access_request_requires_email_for_anonymous(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    monkeypatch.setattr(api_app, "get_auth_store", lambda: store)
    app.dependency_overrides[get_auth_store] = lambda: store
    try:
        client = TestClient(app, base_url="https://testserver")
        res = client.post("/auth/request-access", json={})
    finally:
        app.dependency_overrides.pop(get_auth_store, None)

    assert res.status_code == 422
