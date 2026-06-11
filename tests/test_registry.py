"""Tests for the SQLite Registry."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.registry import Registry


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "registry.db")


def test_registry_seed_defaults(registry: Registry) -> None:
    user = registry.get_user("admin")
    assert user is not None
    assert user["name"] == "Admin"
    assert user["role"] == "org_admin"


def test_registry_backfills_legacy_user_products(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    registry = Registry(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO users (id, name, role, products_js) VALUES (?,?,?,?)",
            ("u1", "User", "viewer", '["prod-a"]'),
        )

    registry = Registry(db)

    assert registry.list_product_ids_for_user("u1") == ["prod-a"]
    assert registry.list_product_memberships("u1") == {"prod-a": "owner"}


def test_registry_products(registry: Registry) -> None:
    product = {
        "id": "test-prod",
        "name": "Test Product",
        "tagline": "A product for testing",
        "owner": {"team": "QA", "lead": "Bob"},
        "onboardedAt": "2026-05-23T12:00:00Z",
    }
    registry.upsert_product(product)

    loaded = registry.get_product("test-prod")
    assert loaded is not None
    assert loaded["name"] == "Test Product"
    assert loaded["owner"] == {"team": "QA", "lead": "Bob"}

    prods = registry.list_products()
    assert len(prods) == 1
    assert prods[0]["id"] == "test-prod"


def test_registry_sources_id_and_name_lookup(registry: Registry) -> None:
    source = {
        "product": "test-prod",
        "name": "my-filesystem-source",
        "type": "filesystem",
        "config": {"roots": ["/tmp/code"]},
    }
    registry.upsert_source(source)

    sources = registry.list_sources("test-prod")
    assert len(sources) == 1

    stored = sources[0]
    sid = stored["id"]
    assert sid.startswith("src_")
    assert stored["name"] == "my-filesystem-source"
    assert stored["type"] == "filesystem"

    by_name = registry.get_source("test-prod", "my-filesystem-source")
    assert by_name is not None
    assert by_name["id"] == sid

    by_id = registry.get_source("test-prod", sid)
    assert by_id is not None
    assert by_id["name"] == "my-filesystem-source"

    assert registry.delete_source("test-prod", sid) is True
    assert registry.get_source("test-prod", sid) is None
    assert registry.get_source("test-prod", "my-filesystem-source") is None


def test_registry_resource_manifest_roundtrip(registry: Registry) -> None:
    registry.upsert_resource_manifest(
        {
            "product": "p",
            "sourceKey": "src",
            "resourceUri": "file.py",
            "contentHash": "abc",
            "mime": "text/x-python",
            "sizeBytes": 123,
            "lastSeenSync": "sync-1",
            "chunkIds": ["c1", "c2"],
            "indexedAt": "now",
            "embeddingVersion": "v1",
        }
    )

    row = registry.get_resource_manifest("p", "src", "file.py")
    assert row is not None
    assert row["contentHash"] == "abc"
    assert row["chunkIds"] == ["c1", "c2"]
    assert registry.list_resource_manifests("p", "src")[0]["resourceUri"] == "file.py"
    assert registry.delete_resource_manifest("p", "src", "file.py") is True
    assert registry.get_resource_manifest("p", "src", "file.py") is None


def test_registry_refuses_plaintext_source_secrets(registry: Registry, monkeypatch) -> None:
    from nexus.auth.token_cipher import TokenCipherError

    monkeypatch.delenv("NEXUS_TOKEN_KEY", raising=False)

    with pytest.raises(TokenCipherError, match="NEXUS_TOKEN_KEY is required"):
        registry.upsert_source(
            {
                "product": "test-prod",
                "name": "insecure-source",
                "type": "github",
                "config": {"token": "secret-token", "repos": ["a/b"]},
            }
        )


def test_registry_source_encryption(registry: Registry, monkeypatch) -> None:
    from nexus.auth.token_cipher import TokenCipher

    key = TokenCipher.generate_key()
    monkeypatch.setenv("NEXUS_TOKEN_KEY", key)

    source = {
        "product": "test-prod",
        "name": "secure-source",
        "type": "github",
        "config": {"token": "my-ultra-secret-token", "repos": ["a/b"]},
    }
    registry.upsert_source(source)

    # Reading should automatically decrypt
    loaded = registry.get_source("test-prod", "secure-source")
    assert loaded is not None
    assert loaded["config"]["token"] == "my-ultra-secret-token"

    # Direct database verification to ensure it's encrypted at rest
    with registry._conn() as conn:
        row = conn.execute(
            "SELECT config_js FROM sources WHERE name = ?", ("secure-source",)
        ).fetchone()
        assert row is not None
        config_data = row["config_js"]
        assert "enc:" in config_data
        assert "my-ultra-secret-token" not in config_data
