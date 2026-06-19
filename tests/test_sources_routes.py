"""Tests for product-scoped source routes and GitHub multi-repo sync."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from nexus.api.app import app
from nexus.api.deps import get_config_dep, get_registry
from nexus.api.routes import sources
from nexus.auth.token_cipher import TokenCipher
from nexus.config import NexusConfig
from nexus.ingest.pipeline import IngestStats
from nexus.registry import Registry


def _config(tmp_path: Path) -> NexusConfig:
    return NexusConfig(
        models={
            "council": {"provider": "test", "model": "test"},
            "light": {"provider": "test", "model": "test"},
            "embedding": {"provider": "test", "model": "test", "url": "http://embed"},
            "reranker": {"provider": "test", "model": "test", "url": "http://rerank"},
        },
        storage={
            "proposal_queue": tmp_path / "proposals.db",
            "council_checkpoint": tmp_path / "council.sqlite",
        },
    )


def test_add_source_refuses_plaintext_secret_without_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NEXUS_TOKEN_KEY", raising=False)
    registry = Registry(tmp_path / "registry.db")
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_config_dep] = lambda: _config(tmp_path)
    try:
        client = TestClient(app)
        r = client.post(
            "/products/demo/sources",
            json={
                "name": "github",
                "type": "github",
                "config": {
                    "token": "ghp_secret",
                    "repos": ["https://github.com/acme/api"],
                },
            },
        )
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_config_dep, None)

    assert r.status_code == 400
    assert "NEXUS_TOKEN_KEY is required" in r.json()["detail"]


def test_filesystem_source_rejected_in_production(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_ENABLE_LOCAL_FS_SOURCES", "false")
    registry = Registry(tmp_path / "registry.db")
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_config_dep] = lambda: _config(tmp_path)
    try:
        client = TestClient(app)
        r = client.post(
            "/products/demo/sources",
            json={
                "name": "local",
                "type": "filesystem",
                "config": {"root": str(tmp_path)},
            },
        )
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_config_dep, None)

    assert r.status_code == 403
    assert r.json()["detail"] == "filesystem sources are disabled"


def test_clone_error_redacts_github_token() -> None:
    text = "fatal: https://x-access-token:ghp_secret@github.com/acme/api.git failed"
    assert "ghp_secret" not in sources._redact_text(text)


def test_github_repo_urls_validate_all_before_clone() -> None:
    source = {
        "config": {
            "repos": [
                "https://github.com/acme/api",
                "not-a-github-url",
            ]
        }
    }

    try:
        sources._github_repo_urls(source)
    except ValueError as e:
        assert "not-a-github-url" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected invalid repo URL to fail")


def test_github_sync_clones_all_repos_and_aggregates_count(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("NEXUS_TOKEN_KEY", TokenCipher.generate_key())
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    runtime = {
        "product": "demo",
        "name": "github",
        "type": "github",
        "status": "connected",
        "config": {
            "token": "ghp_secret",
            "repos": [
                "https://github.com/acme/api",
                "git@github.com:acme/web",
            ],
        },
        "resourceCount": 0,
    }
    registry.upsert_source(runtime)
    runtime = registry.get_source("demo", "github")
    assert runtime is not None

    cloned: list[str] = []

    async def fake_clone(url, token, q, index, total):
        cloned.append(url)
        root = tmp_path / f"repo-{index}"
        root.mkdir()
        return root, root

    async def fake_ingest_root(*, root_label, **kwargs):
        count = 2 if root_label.endswith("/api") else 3
        return IngestStats(resources_seen=count, resources_indexed=count), None

    monkeypatch.setattr(sources, "_clone_github_repo", fake_clone)
    monkeypatch.setattr(sources, "_ingest_root", fake_ingest_root)

    asyncio.run(
        sources._sync_source_contents(
            product_id="demo",
            source=runtime,
            runtime=runtime,
            config=cfg,
            registry=registry,
            q=asyncio.Queue(),
        )
    )

    assert cloned == ["https://github.com/acme/api", "git@github.com:acme/web"]
    updated = registry.get_source("demo", "github")
    assert updated is not None
    assert updated["resourceCount"] == 5


def test_jira_sync_uses_direct_source_and_updates_count(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_TOKEN_KEY", TokenCipher.generate_key())
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    runtime = {
        "product": "demo",
        "name": "jira",
        "type": "jira",
        "status": "connected",
        "config": {
            "site_url": "https://example.atlassian.net",
            "email": "me@example.com",
            "api_token": "tok",
            "jql": "project = AUTH",
        },
        "resourceCount": 0,
    }
    registry.upsert_source(runtime)
    runtime = registry.get_source("demo", "jira")
    assert runtime is not None

    async def fake_ingest_jira_source(**kwargs):
        assert kwargs["source"]["type"] == "jira"
        return IngestStats(resources_seen=4, resources_indexed=4)

    monkeypatch.setattr(sources, "_ingest_jira_source", fake_ingest_jira_source)

    asyncio.run(
        sources._sync_source_contents(
            product_id="demo",
            source=runtime,
            runtime=runtime,
            config=cfg,
            registry=registry,
            q=asyncio.Queue(),
        )
    )

    updated = registry.get_source("demo", "jira")
    assert updated is not None
    assert updated["resourceCount"] == 4
    assert updated["status"] == "connected"


def test_sync_source_dedupes_in_flight_runs(tmp_path: Path, monkeypatch) -> None:
    registry = Registry(tmp_path / "registry.db")
    root = tmp_path / "repo"
    root.mkdir()
    registry.upsert_source({
        "product": "demo",
        "name": "local",
        "type": "filesystem",
        "status": "connected",
        "config": {"root": str(root)},
        "resourceCount": 1,
    })
    cfg = _config(tmp_path)
    started = 0

    async def fake_sync_source_contents(**kwargs):
        nonlocal started
        started += 1
        await asyncio.sleep(0.2)

    monkeypatch.setattr(sources, "_sync_source_contents", fake_sync_source_contents)
    sources._sync_tasks.clear()
    async def scenario():
        first = await sources.sync_source(
            product_id="demo", source_id="local", config=cfg, registry=registry
        )
        second = await sources.sync_source(
            product_id="demo", source_id="local", config=cfg, registry=registry
        )
        await asyncio.sleep(0.25)
        return first, second

    try:
        first, second = asyncio.run(scenario())
    finally:
        sources._sync_tasks.clear()

    assert first["queued"] is True
    assert second["already_running"] is True
    assert started == 1


def test_confluence_sync_uses_direct_source_and_updates_count(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("NEXUS_TOKEN_KEY", TokenCipher.generate_key())
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    runtime = {
        "product": "demo",
        "name": "confluence",
        "type": "confluence",
        "status": "connected",
        "config": {
            "site_url": "https://example.atlassian.net",
            "email": "me@example.com",
            "api_token": "tok",
            "space_keys": ["DOCS"],
        },
        "resourceCount": 0,
    }
    registry.upsert_source(runtime)
    runtime = registry.get_source("demo", "confluence")
    assert runtime is not None

    async def fake_ingest_confluence_source(**kwargs):
        assert kwargs["source"]["type"] == "confluence"
        return IngestStats(resources_seen=7, resources_indexed=7)

    monkeypatch.setattr(sources, "_ingest_confluence_source", fake_ingest_confluence_source)

    asyncio.run(
        sources._sync_source_contents(
            product_id="demo",
            source=runtime,
            runtime=runtime,
            config=cfg,
            registry=registry,
            q=asyncio.Queue(),
        )
    )

    updated = registry.get_source("demo", "confluence")
    assert updated is not None
    assert updated["resourceCount"] == 7
    assert updated["status"] == "connected"
