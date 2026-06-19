"""Live backend E2E: ingest -> vector store -> LLM smoke -> approve/reject.

This test intentionally uses real configured infrastructure. It has no mocks,
fakes, or monkeypatches. Run it after backend changes with:

    NEXUS_LIVE_E2E=1 uv run pytest -q -m live_e2e

Required live services/config:
  - `nexus.yaml`
  - Qdrant reachable at `vector_store.url`
  - embedder reachable at `models.embedding.url`
  - reranker reachable at `models.reranker.url`
  - FalkorDB reachable at `graph_store.host:graph_store.port`
  - council/light LLM credentials configured
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from git import Repo

from nexus.api.app import app
from nexus.api.deps import get_config_dep, get_proposal_queue, get_registry
from nexus.config import NexusConfig
from nexus.connectors.local_fs import LocalFsConfig, LocalFsSource
from nexus.council.queue import ProposalQueue
from nexus.ingest.indexer_factory import create_indexer
from nexus.ingest.pipeline import run_ingest
from nexus.llm.client import ChatClient
from nexus.registry import Registry
from nexus.retrieval.pipeline import RetrievalContext, retrieve
from nexus.skills.models import Citation, SkillProposal

pytestmark = pytest.mark.live_e2e


def test_live_backend_e2e_ingest_council_review_flows(tmp_path: Path) -> None:
    _require_live_e2e()
    config = _live_config(tmp_path)
    _require_infra(config)

    product_id = f"live-e2e-qdrant-{uuid.uuid4().hex[:8]}"
    source_root = _write_fixture_product(tmp_path / "fixture-product")
    _init_live_skills_repo(config.hierarchy_root)
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
    assert stats.resources_seen >= 7
    assert stats.resources_indexed >= 7
    assert stats.embed_errors == 0

    counts = asyncio.run(_vector_counts(config, product_id))
    assert counts["code"] > 0
    assert counts["text"] > 0

    hits = asyncio.run(_live_retrieval_smoke(config, product_id))
    assert hits >= 1

    token_count = asyncio.run(_live_council_model_smoke(config))
    assert token_count >= 0

    master = _proposal(
        product_id=product_id,
        name="ledger-api-master",
        tier="product_master",
        body=(
            "# Ledger API Master\n\n"
            "Use account-scoped ledger reads and cite API evidence. [file:openapi-notes.md:1]"
        ),
    )
    focused = _proposal(
        product_id=product_id,
        name="ledger-api-testing",
        tier="quality_security",
        body=(
            "# Ledger API Testing\n\n"
            "Assert status codes and JSON response bodies. [file:security-and-testing.md:1]"
        ),
    )
    for proposal in (master, focused):
        queue.enqueue(proposal, session_id=f"cs_{product_id}", product_id=product_id)

    pending = queue.list(status="pending", product_id=product_id)
    assert any(p["tier"] == "product_master" for p in pending)
    assert len(pending) >= 2
    for proposal in pending:
        assert proposal["citations"], proposal["name"]
        assert "[file:" in proposal["body"]

    app.dependency_overrides[get_proposal_queue] = lambda: queue
    app.dependency_overrides[get_config_dep] = lambda: config
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        client = TestClient(app)

        agent_res = client.post(
            f"/products/{product_id}/agent/messages",
            json={
                "message": "Explain ledger API retrieval architecture and testing guardrails",
                "mode": "drift_lite",
                "top_k": 6,
            },
        )
        assert agent_res.status_code == 200, agent_res.text
        agent_body = agent_res.json()
        assert agent_body["product_id"] == product_id
        assert agent_body["session_id"]
        assert agent_body["citations"], agent_body
        assert agent_body["coverage"]["sufficient"] is True
        assert agent_body["query_plan"]["mode"] == "drift_lite"
        assert agent_body["query_plan"]["latency_ms"] >= 0
        assert any(step["channel"] == "query_plan" for step in agent_body["trace"])
        assert all(step["product_id"] == product_id for step in agent_body["trace"])

        replay_res = client.get(
            f"/products/{product_id}/agent/sessions/{agent_body['session_id']}"
        )
        assert replay_res.status_code == 200, replay_res.text
        replay = replay_res.json()
        assert replay["product_id"] == product_id
        assert [message["role"] for message in replay["messages"]] == [
            "user",
            "assistant",
        ]

        master = next(p for p in pending if p["tier"] == "product_master")
        approve_res = client.post(
            f"/proposals/{master['id']}/approve",
            json={"actor": "live-e2e@nexus.local"},
        )
        assert approve_res.status_code == 200, approve_res.text
        approve_body = approve_res.json()
        assert approve_body["ok"] is True
        assert (config.hierarchy_root / approve_body["path"]).exists()
        assert approve_body["skill_index_status"] in {"indexed", "pending"}
        assert queue.get(master["id"])["status"] == "approved"

        reject_target = next(p for p in pending if p["id"] != master["id"])
        reject_res = client.post(
            f"/proposals/{reject_target['id']}/reject",
            json={"reason": "Live E2E rejection scenario."},
        )
        assert reject_res.status_code == 200, reject_res.text
        assert queue.get(reject_target["id"])["status"] == "rejected"

    finally:
        app.dependency_overrides.pop(get_proposal_queue, None)
        app.dependency_overrides.pop(get_config_dep, None)
        app.dependency_overrides.pop(get_registry, None)


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
                    "proposal_queue": tmp_path / "qdrant" / "proposals.db",
                    "council_checkpoint": tmp_path / "qdrant" / "council.sqlite",
                }
            ),
        }
    )


def _require_infra(config: NexusConfig) -> None:
    targets = [("qdrant", config.vector_store.url)]
    if config.models.embedding.provider == "jina-local":
        targets.append(("embedder", config.models.embedding.url or "http://localhost:8080"))
    if config.models.reranker.provider == "jina-local":
        targets.append(("reranker", config.models.reranker.url or "http://localhost:8081"))
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
    _require_tcp(
        "falkordb",
        config.graph_store.host,
        config.graph_store.port,
        timeout_s=45,
    )


def _require_tcp(name: str, host: str, port: int, *, timeout_s: int) -> None:
    last_error: Exception | None = None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3.0):
                return
        except Exception as e:
            last_error = e
        time.sleep(1.0)
    pytest.fail(f"{name} live infra unreachable at {host}:{port}: {last_error}")


def _write_fixture_product(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "README.md").write_text(
        """# Ledger API Fixture

Ledger API manages accounts and ledger entries for enterprise finance workflows.
Accounts own many entries; every entry belongs to exactly one account.

Operational guardrails:
- API handlers validate account identifiers before reading entries.
- Tests must cover HTTP status codes and response bodies.
- Local setup command: `uv run pytest -q`.
- CI command: `uv run ruff check . && uv run pytest -q`.
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

Successful response body:
```json
{"account_id":"acct-1","entries":[{"id":"entry-1","amount":1250,"currency":"USD","posted_at":"2026-01-15"}]}
```

Ledger entry schema:
- `id`: string entry identifier.
- `amount`: integer minor units.
- `currency`: three-letter ISO currency code.
- `posted_at`: ISO date string.

Malformed account identifiers return HTTP 400 with
`{"detail":"invalid account id"}`. Unknown well-formed identifiers return HTTP
404 with `{"detail":"account not found"}`. List responses are capped at 100
entries per account until pagination is implemented.
""",
        encoding="utf-8",
    )
    (root / "architecture.md").write_text(
        """# Architecture

The fixture is a single-process FastAPI service with in-memory account storage.
Production deployments must replace the `ACCOUNTS` dictionary with durable
storage and keep request handlers thin: validation at the HTTP boundary,
ledger lookup in a service layer, and response shaping in the route.

The service is intentionally read-only in this fixture. Write operations are
out of scope until idempotency keys, transaction boundaries, and audit logging
are designed.

Future write endpoints must require an `Idempotency-Key` header, write one audit
record per ledger mutation, and reject duplicate keys with the original result.
The first production storage adapter must expose account-scoped lookup APIs so
route code cannot cross account boundaries by accident.
""",
        encoding="utf-8",
    )
    (root / "security-and-testing.md").write_text(
        """# Security and Testing

Security requirements:
- Validate account identifiers before any ledger lookup.
- Return HTTP 404 for unknown accounts without leaking other account data.
- Add bearer-token authentication and account-level authorization before production use.
- Rate limit reads to 60 requests per minute per account token.
- Keep ledger entries scoped to exactly one account.

Testing requirements:
- Cover success and missing-account HTTP status codes.
- Assert response bodies, not just status codes.
- Add negative tests for malformed account identifiers.
- Add service-layer tests when durable storage replaces in-memory fixtures.
- Run `uv run pytest -q` locally and in CI.
- CI pipeline also runs `uv run ruff check .`.
- Minimum coverage threshold is 85% for route and service modules.
""",
        encoding="utf-8",
    )
    (root / "reference-patterns.md").write_text(
        """# Reference Patterns

Recommended patterns:
- Route handlers should check account existence before returning ledger entries.
- Missing accounts should raise `HTTPException(status_code=404)`.
- Tests should use `TestClient(app)` and assert both status code and JSON body.
- Future durable-storage code should keep ledger lookup behind a service boundary.
- Authentication must happen before ledger lookup; authorization checks must
  bind the token to the requested account id.
- Rate-limit checks should run after authentication and before service lookup.

Patterns to avoid:
- Do not expose entries across account boundaries.
- Do not add write endpoints until idempotency and audit logging exist.
- Do not return more than 100 entries from one read response without pagination.
""",
        encoding="utf-8",
    )
    return root


def _init_live_skills_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(root)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Nexus Live E2E")
        writer.set_value("user", "email", "live-e2e@nexus.local")


async def _vector_counts(config: NexusConfig, product_id: str) -> dict[str, int]:
    indexer = create_indexer(config)
    try:
        return {
            "code": await indexer.count(product_id=product_id, vector_kind="code"),
            "text": await indexer.count(product_id=product_id, vector_kind="text"),
        }
    finally:
        await indexer.aclose()


async def _live_retrieval_smoke(config: NexusConfig, product_id: str) -> int:
    ctx = RetrievalContext.from_config(config)
    try:
        result = await retrieve(
            ctx=ctx,
            product_id=product_id,
            query="ledger entries response body testing security",
            top_k=3,
            mode="auto",
        )
        return len(result.hits)
    finally:
        await ctx.aclose()


async def _live_council_model_smoke(config: NexusConfig) -> int:
    tokens: list[str] = []

    async def token_sink(token: dict[str, str]) -> None:
        text = token.get("text") or ""
        if text:
            tokens.append(text)

    client = ChatClient.from_cfg(
        config.models.council,
        role="live-e2e",
        token_sink=token_sink,
    )
    try:
        response = await client.chat(
            [
                {
                    "role": "user",
                    "content": "Reply with exactly: live e2e ok",
                }
            ],
            temperature=0.0,
            max_tokens=24,
        )
        assert "live" in response.content.lower()
        return len(tokens)
    finally:
        await client.aclose()


def _proposal(
    *,
    product_id: str,
    name: str,
    tier: str,
    body: str,
) -> SkillProposal:
    return SkillProposal(
        id=f"prop_{product_id}_{name}",
        name=name,
        tier=tier,
        body=body,
        citations=[
            Citation(
                file="openapi-notes.md",
                line=1,
                excerpt="GET /accounts/{account_id}/entries returns entries.",
            )
        ],
        confidence=0.9,
        created_at=datetime.now(UTC).isoformat(),
    )
