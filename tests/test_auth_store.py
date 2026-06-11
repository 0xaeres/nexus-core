from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.auth.store import AuthError, AuthStore, hash_password


def test_password_hash_uses_argon2_and_unique_salts() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert first.startswith("$argon2")
    assert second.startswith("$argon2")
    assert first != second
    assert "correct horse" not in first


def test_login_verifies_password_and_stores_hashed_session(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.db", secret_key="session-secret")
    store.create_user(
        email="owner@example.com",
        password="correct horse battery staple",
        role="admin",
    )

    with pytest.raises(AuthError):
        store.login(email="owner@example.com", password="wrong password")

    login = store.login(
        email="owner@example.com", password="correct horse battery staple"
    )
    assert login.user["email"] == "owner@example.com"
    assert store.user_for_session(login.session_token) is not None

    with sqlite3.connect(tmp_path / "auth.db") as conn:
        row = conn.execute("SELECT token_hash FROM auth_sessions").fetchone()
    assert row is not None
    assert row[0] != login.session_token
    assert len(row[0]) == 64


def test_access_request_approval_creates_approved_user(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.db", secret_key="session-secret")
    req = store.request_access(email="dev@example.com", name="Dev")

    with pytest.raises(AuthError):
        store.decide_access_request(
            req["id"], status="approved", decided_by="owner@example.com"
        )
    assert store.get_access_request(req["id"])["status"] == "pending"

    store.decide_access_request(
        req["id"],
        status="approved",
        decided_by="owner@example.com",
        password="correct horse battery staple",
        role="viewer",
    )
    user = store.get_user_by_email("dev@example.com")
    assert user is not None
    assert user["status"] == "approved"
    assert user["role"] == "viewer"


def test_existing_auth0_user_matching_bootstrap_email_is_promoted(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("NEXUS_BOOTSTRAP_ADMIN_EMAIL", "owner@example.com")
    store = AuthStore(tmp_path / "auth.db", secret_key="session-secret")
    store.create_user(
        email="owner@example.com",
        password="correct horse battery staple",
        role="viewer",
        status="pending",
    )

    user = store.get_or_create_auth0_user(
        auth_sub="auth0|owner", email="owner@example.com", name="Owner"
    )

    assert user["role"] == "admin"
    assert user["status"] == "approved"
    assert user["auth_sub"] == "auth0|owner"
