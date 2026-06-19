from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

import nexus.api.app as api_app
from nexus.api.app import app
from nexus.api.deps import get_auth_store, get_proposal_queue, get_registry, get_skill_store
from nexus.auth.store import AuthStore
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.skills.models import Citation, SkillProposal
from nexus.skills.store import SkillStore


def test_bootstrap_admin_from_env_creates_password_user(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("NEXUS_BOOTSTRAP_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("NEXUS_BOOTSTRAP_ADMIN_PASSWORD", "correct horse battery staple")
    store = AuthStore(tmp_path / "auth.db", secret_key="secret")

    owner = store.get_user_by_email("owner@example.com")

    assert owner is not None
    assert owner["role"] == "admin"
    assert owner["status"] == "approved"
    login = store.login(
        email="owner@example.com", password="correct horse battery staple"
    )
    assert login.user["id"] == owner["id"]


def test_product_owner_can_manage_only_their_product(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    auth_store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    user = auth_store.create_user(
        email="owner@example.com",
        password="correct horse battery staple",
        role="viewer",
    )
    registry = Registry(tmp_path / "registry.db")
    registry.upsert_product(
        {"id": "own", "name": "Own", "tagline": "", "owner": {}, "onboardedAt": "now"}
    )
    registry.upsert_product(
        {"id": "other", "name": "Other", "tagline": "", "owner": {}, "onboardedAt": "now"}
    )
    registry.grant_product_role("own", user["id"], "owner")
    queue = ProposalQueue(tmp_path / "queue.db")
    skill_store = SkillStore(tmp_path / "skills")

    monkeypatch.setattr(api_app, "get_auth_store", lambda: auth_store)
    app.dependency_overrides[get_auth_store] = lambda: auth_store
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_proposal_queue] = lambda: queue
    app.dependency_overrides[get_skill_store] = lambda: skill_store
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
        assert login.status_code == 200

        products = client.get("/products")
        assert [p["id"] for p in products.json()["products"]] == ["own"]

        own = client.post(
            "/products/own/sources",
            json={"name": "github", "type": "github", "config": {"repos": ["https://github.com/a/b"]}},
            headers={"X-Nexus-CSRF": csrf or ""},
        )
        other = client.post(
            "/products/other/sources",
            json={"name": "github", "type": "github", "config": {"repos": ["https://github.com/a/b"]}},
            headers={"X-Nexus-CSRF": csrf or ""},
        )
    finally:
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_proposal_queue, None)
        app.dependency_overrides.pop(get_skill_store, None)

    assert own.status_code == 200
    assert other.status_code == 403


def test_viewer_cannot_approve_product_proposal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_SECRET_KEY", "test-secret")
    store = AuthStore(tmp_path / "auth.db", secret_key="test-secret")
    user = store.create_user(
        email="viewer@example.com",
        password="correct horse battery staple",
        role="viewer",
    )
    registry = Registry(tmp_path / "registry.db")
    registry.upsert_product(
        {"id": "demo", "name": "Demo", "tagline": "", "owner": {}, "onboardedAt": "now"}
    )
    registry.grant_product_role("demo", user["id"], "viewer")
    queue = ProposalQueue(tmp_path / "queue.db")
    proposal = SkillProposal(
        id="prop_demo",
        name="Demo",
        body="body",
        citations=[Citation(file="a.py", line=1)],
        confidence=0.7,
        created_at=datetime.now(UTC).isoformat(),
    )
    queue.enqueue(proposal, session_id="cs_demo", product_id="demo")

    monkeypatch.setattr(api_app, "get_auth_store", lambda: store)
    app.dependency_overrides[get_auth_store] = lambda: store
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_proposal_queue] = lambda: queue
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
        res = client.post(
            "/proposals/prop_demo/reject",
            json={"reason": "no"},
            headers={"X-Nexus-CSRF": csrf or ""},
        )
    finally:
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_proposal_queue, None)

    assert res.status_code == 403
