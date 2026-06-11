from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import nexus.api.app as api_app
from nexus.api.app import app
from nexus.api.deps import get_auth_store, get_proposal_queue, get_registry, get_skill_store
from nexus.auth.auth0 import Auth0Error, Auth0Verifier
from nexus.auth.store import AuthStore
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.skills.models import Citation, SkillProposal
from nexus.skills.store import SkillStore


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _SigningKey:
    def __init__(self, key):
        self.key = key.public_key()


def _token(key, *, audience: str = "api", issuer: str = "https://tenant.auth0.com/"):
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": "auth0|user-1",
            "email": "dev@example.com",
            "name": "Dev User",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        key,
        algorithm="RS256",
        headers={"kid": "test"},
    )


def test_auth0_verifier_accepts_valid_rs256_token(monkeypatch) -> None:
    key = _rsa_key()
    verifier = Auth0Verifier(
        domain="tenant.auth0.com",
        audience="api",
        issuer="https://tenant.auth0.com/",
    )
    monkeypatch.setattr(
        verifier._jwks, "get_signing_key_from_jwt", lambda _token: _SigningKey(key)
    )

    claims = verifier.verify(_token(key))

    assert claims.sub == "auth0|user-1"
    assert claims.email == "dev@example.com"


def test_auth0_verifier_rejects_wrong_audience(monkeypatch) -> None:
    key = _rsa_key()
    verifier = Auth0Verifier(
        domain="tenant.auth0.com",
        audience="api",
        issuer="https://tenant.auth0.com/",
    )
    monkeypatch.setattr(
        verifier._jwks, "get_signing_key_from_jwt", lambda _token: _SigningKey(key)
    )

    with pytest.raises(Auth0Error):
        verifier.verify(_token(key, audience="other-api"))


def test_auth0_bootstrap_admin_only_for_matching_email(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_BOOTSTRAP_ADMIN_EMAIL", "owner@example.com")
    store = AuthStore(tmp_path / "auth.db", secret_key="secret")

    owner = store.get_or_create_auth0_user(
        auth_sub="auth0|owner", email="owner@example.com", name="Owner"
    )
    pending = store.get_or_create_auth0_user(
        auth_sub="auth0|dev", email="dev@example.com", name="Dev"
    )

    assert owner["role"] == "admin"
    assert owner["status"] == "approved"
    assert pending["role"] == "viewer"
    assert pending["status"] == "pending"


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
