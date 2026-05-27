"""Live backend E2E: ingest -> vector store -> council -> review actions.

This test intentionally uses real configured infrastructure. It has no mocks,
fakes, or monkeypatches. Run it after backend changes with:

    NEXUS_LIVE_E2E=1 uv run pytest -q -m live_e2e

Required live services/config:
  - `nexus.yaml`
  - Qdrant reachable at `vector_store.url`
  - embedder reachable at `models.embedding.url`
  - reranker reachable at `models.reranker.url`
  - council/light LLM credentials configured
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from nexus.api.app import app
from nexus.api.deps import get_config_dep, get_proposal_queue
from nexus.config import NexusConfig
from nexus.connectors.local_fs import LocalFsConfig, LocalFsSource
from nexus.council import runner
from nexus.council.queue import ProposalQueue
from nexus.ingest.indexer import Indexer
from nexus.ingest.pipeline import run_ingest
from nexus.registry import Registry

pytestmark = pytest.mark.live_e2e


def test_live_backend_e2e_ingest_council_review_flows(tmp_path: Path) -> None:
    _require_live_e2e()
    config = _live_config(tmp_path)
    _require_infra(config)

    product_id = f"live-e2e-{uuid.uuid4().hex[:8]}"
    source_root = _write_fixture_product(tmp_path / "fixture-product")
    registry = Registry(tmp_path / "registry.db")
    queue = ProposalQueue(config.storage.proposal_queue)

    stats = asyncio.run(
        run_ingest(
            product_id=product_id,
            source=LocalFsSource(LocalFsConfig(root=source_root)),
            config=config,
            enrich=False,
            registry=registry,
            source_key="fixture-product",
        )
    )
    assert stats.resources_seen >= 4
    assert stats.resources_indexed >= 4
    assert stats.embed_errors == 0

    counts = asyncio.run(_vector_counts(config, product_id))
    assert counts["code"] > 0
    assert counts["text"] > 0

    asyncio.run(
        asyncio.wait_for(
            runner._run_session(
                config=config,
                queue=queue,
                session_id=f"cs_{product_id}",
                product_id=product_id,
                topic=(
                    "Generate a compact product skill pack for the live E2E fixture. "
                    "Return exactly one product master skill and three focused skills. "
                    "Cover architecture, API surface, domain vocabulary, testing, and security."
                ),
            ),
            timeout=420,
        )
    )
    session = queue.get_session(f"cs_{product_id}")
    assert session is not None
    assert session["status"] == "completed"
    assert len(session["proposal_ids"]) >= 3

    pending = queue.list(status="pending", product_id=product_id)
    assert any(p["tier"] == "product_master" for p in pending)
    assert len(pending) >= 3
    for proposal in pending:
        assert proposal["citations"], proposal["name"]
        assert "[file:" in proposal["body"]

    app.dependency_overrides[get_proposal_queue] = lambda: queue
    app.dependency_overrides[get_config_dep] = lambda: config
    try:
        client = TestClient(app)

        master = next(p for p in pending if p["tier"] == "product_master")
        approve_res = client.post(
            f"/proposals/{master['id']}/approve",
            json={"actor": "live-e2e@nexus.local"},
        )
        assert approve_res.status_code == 200, approve_res.text
        assert approve_res.json()["ok"] is True
        assert (config.hierarchy_root / product_id / f"{master['name']}.skill.md").exists()
        assert queue.get(master["id"])["status"] == "approved"

        reject_target = next(p for p in pending if p["id"] != master["id"])
        reject_res = client.post(
            f"/proposals/{reject_target['id']}/reject",
            json={"reason": "Live E2E rejection scenario."},
        )
        assert reject_res.status_code == 200, reject_res.text
        assert queue.get(reject_target["id"])["status"] == "rejected"

        revise_target = next(
            p for p in pending if p["id"] not in {master["id"], reject_target["id"]}
        )
        revise_res = client.post(
            f"/proposals/{revise_target['id']}/revise",
            json={
                "summary": "Add more exact test command and API contract detail.",
                "comments": [
                    {"line": 5, "body": "Mention pytest route coverage explicitly."}
                ],
            },
        )
        assert revise_res.status_code == 200, revise_res.text
        revision_session = revise_res.json()["session_id"]
        assert queue.get(revise_target["id"])["status"] == "revision_requested"
        revision_topic = (
            f"Revise skill proposal `{revise_target['name']}`.\n\n"
            "SME requested changes:\n"
            "Add more exact test command and API contract detail.\n\n"
            "Line comments:\n"
            "- line 5: Mention pytest route coverage explicitly.\n\n"
            f"Previous draft:\n{revise_target['body']}"
        )
    finally:
        app.dependency_overrides.pop(get_proposal_queue, None)
        app.dependency_overrides.pop(get_config_dep, None)

    asyncio.run(
        asyncio.wait_for(
            runner._run_session(
                config=config,
                queue=queue,
                session_id=revision_session,
                product_id=product_id,
                topic=revision_topic,
            ),
            timeout=420,
        )
    )
    revised_session = queue.get_session(revision_session)
    assert revised_session is not None
    assert revised_session["status"] == "completed"
    assert revised_session["proposal_ids"]
    revised = queue.get(revised_session["proposal_ids"][0])
    assert revised is not None
    assert revised["status"] == "pending"
    assert revised["citations"]


def _require_live_e2e() -> None:
    if os.environ.get("NEXUS_LIVE_E2E") != "1":
        pytest.skip("set NEXUS_LIVE_E2E=1 to run real live E2E")


def _live_config(tmp_path: Path) -> NexusConfig:
    path = Path(os.environ.get("NEXUS_LIVE_E2E_CONFIG", "nexus.yaml"))
    if not path.exists():
        pytest.fail(f"live E2E config not found: {path}")
    config = NexusConfig.load(path)
    fast_models = config.models.model_copy(
        update={
            "council": config.models.light,
            "drafter": config.models.light,
            "critic": config.models.light,
            "reviser": config.models.light,
        }
    )
    return config.model_copy(
        update={
            "hierarchy_root": tmp_path / "skills",
            "models": fast_models,
            "storage": config.storage.model_copy(
                update={
                    "proposal_queue": tmp_path / "proposals.db",
                    "council_checkpoint": tmp_path / "council.sqlite",
                }
            ),
        }
    )


def _require_infra(config: NexusConfig) -> None:
    targets = [
        ("qdrant", config.vector_store.url),
        ("embedder", config.models.embedding.url or "http://localhost:8080"),
        ("reranker", config.models.reranker.url or "http://localhost:8081"),
    ]
    for name, url in targets:
        last_error: Exception | None = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=3.0) as client:
                    res = client.get(url)
                    if res.status_code < 500:
                        last_error = None
                        break
            except Exception as e:
                last_error = e
            time.sleep(1.0)
        if last_error is not None:
            pytest.fail(f"{name} live infra unreachable at {url}: {last_error}")


def _write_fixture_product(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "README.md").write_text(
        """# Ledger API Fixture

Ledger API manages accounts and ledger entries for enterprise finance workflows.
Accounts own many entries; every entry belongs to exactly one account.

Operational guardrails:
- API handlers validate account identifiers before reading entries.
- Tests must cover HTTP status codes and response bodies.
""",
        encoding="utf-8",
    )
    (root / "app.py").write_text(
        '''from fastapi import FastAPI, HTTPException

app = FastAPI()

ACCOUNTS = {"acct-1": [{"id": "entry-1", "amount": 1250}]}


@app.get("/accounts/{account_id}/entries")
def list_entries(account_id: str):
    if account_id not in ACCOUNTS:
        raise HTTPException(status_code=404, detail="account not found")
    return {"account_id": account_id, "entries": ACCOUNTS[account_id]}
''',
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        '''from fastapi.testclient import TestClient

from app import app


def test_list_entries_success():
    res = TestClient(app).get("/accounts/acct-1/entries")
    assert res.status_code == 200
    assert res.json()["entries"][0]["id"] == "entry-1"


def test_list_entries_missing_account():
    res = TestClient(app).get("/accounts/missing/entries")
    assert res.status_code == 404
''',
        encoding="utf-8",
    )
    (root / "openapi-notes.md").write_text(
        """# API Surface

`GET /accounts/{account_id}/entries` returns entries for one account. Missing
accounts return HTTP 404 with `account not found`.
""",
        encoding="utf-8",
    )
    return root


async def _vector_counts(config: NexusConfig, product_id: str) -> dict[str, int]:
    indexer = Indexer(url=config.vector_store.url)
    try:
        return {
            "code": await indexer.count(product_id=product_id, vector_kind="code"),
            "text": await indexer.count(product_id=product_id, vector_kind="text"),
        }
    finally:
        await indexer.aclose()

