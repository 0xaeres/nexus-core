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


def test_auth_store_migrates_legacy_user_roles(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.db", secret_key="session-secret")
    user = store.create_user(
        email="legacy@example.com",
        password="correct horse battery staple",
        role="viewer",
    )
    with sqlite3.connect(tmp_path / "auth.db") as conn:
        conn.execute("UPDATE auth_users SET role = 'org_admin' WHERE id = ?", (user["id"],))

    migrated = AuthStore(tmp_path / "auth.db", secret_key="session-secret")
    loaded = migrated.get_user(user["id"])

    assert loaded is not None
    assert loaded["role"] == "admin"
    with sqlite3.connect(tmp_path / "auth.db") as conn:
        role = conn.execute("SELECT role FROM auth_users WHERE id = ?", (user["id"],)).fetchone()[0]
    assert role == "admin"


def test_bootstrap_admin_repairs_existing_matching_user(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("NEXUS_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("NEXUS_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    store = AuthStore(tmp_path / "auth.db", secret_key="session-secret")
    user = store.create_user(
        email="owner@example.com",
        password="correct horse battery staple",
        role="viewer",
        status="pending",
    )
    monkeypatch.setenv("NEXUS_BOOTSTRAP_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("NEXUS_BOOTSTRAP_ADMIN_PASSWORD", "another secure password")
    AuthStore(tmp_path / "auth.db", secret_key="session-secret")

    loaded = store.get_user_by_email("owner@example.com")

    assert loaded is not None
    assert loaded["id"] == user["id"]
    assert loaded["role"] == "admin"
    assert loaded["status"] == "approved"
    repaired = store.login(email="owner@example.com", password="another secure password")
    assert repaired.user["id"] == user["id"]


def test_invalid_password_hash_returns_auth_error(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.db", secret_key="session-secret")
    user = store.create_user(
        email="owner@example.com",
        password="correct horse battery staple",
        role="admin",
    )
    with sqlite3.connect(tmp_path / "auth.db") as conn:
        conn.execute("UPDATE auth_users SET password_hash = '' WHERE id = ?", (user["id"],))

    with pytest.raises(AuthError):
        store.login(email="owner@example.com", password="correct horse battery staple")
