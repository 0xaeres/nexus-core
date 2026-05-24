"""Route-level tests for GET /products/{id}/status — powers dashboard card state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus.api.app import app
from nexus.api.deps import get_proposal_queue, get_registry, get_skill_store
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.skills.models import AppliesTo, Citation, Provenance, Skill, SkillProposal
from nexus.skills.store import SkillStore


@pytest.fixture
def client(tmp_path: Path):
    registry = Registry(tmp_path / "registry.db")
    queue = ProposalQueue(tmp_path / "queue.db")
    store = SkillStore(tmp_path / "skills")

    registry.upsert_product({
        "id": "demo",
        "name": "Demo",
        "tagline": "",
        "owner": {},
        "onboardedAt": datetime.now(UTC).isoformat(),
    })

    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_proposal_queue] = lambda: queue
    app.dependency_overrides[get_skill_store] = lambda: store
    try:
        yield TestClient(app), registry, queue, store
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_proposal_queue, None)
        app.dependency_overrides.pop(get_skill_store, None)


def _add_synced_source(registry: Registry, product_id: str) -> None:
    registry.upsert_source({
        "product": product_id,
        "name": "github",
        "type": "github",
        "status": "connected",
        "config": {"repos": ["acme/repo"]},
        "lastSync": datetime.now(UTC).isoformat(),
        "resourceCount": 42,
    })


def _pending_proposal() -> SkillProposal:
    return SkillProposal(
        id="prop_1",
        name="Demo skill",
        body="body",
        citations=[Citation(file="a.py", line=1)],
        confidence=0.5,
        created_at=datetime.now(UTC).isoformat(),
    )


def _demo_skill(product_id: str) -> Skill:
    return Skill(
        name="demo-overview",
        product=product_id,
        confidence=0.8,
        applies_to=AppliesTo(),
        provenance=Provenance(
            validated_by="jl",
            validated_at=datetime.now(UTC).isoformat(),
        ),
        body="# Demo\n",
    )


def test_status_404s_when_product_missing(client) -> None:
    c, _, _, _ = client
    r = c.get("/products/nope/status")
    assert r.status_code == 404


def test_status_stage_none_when_no_sources(client) -> None:
    c, _, _, _ = client
    body = c.get("/products/demo/status").json()
    assert body == {
        "hasEmbeddings": False,
        "hasSkill": False,
        "councilInProgress": False,
        "currentSessionId": None,
        "currentStage": "none",
    }


def test_status_stage_ingesting_when_source_added_but_no_sync(client) -> None:
    c, registry, _, _ = client
    registry.upsert_source({
        "product": "demo",
        "name": "github",
        "type": "github",
        "status": "connected",
        "config": {},
        "resourceCount": 0,
    })
    body = c.get("/products/demo/status").json()
    assert body["currentStage"] == "ingesting"
    assert body["hasEmbeddings"] is False


def test_status_stage_council_when_embeddings_ready_no_skill(client) -> None:
    c, registry, _, _ = client
    _add_synced_source(registry, "demo")
    body = c.get("/products/demo/status").json()
    assert body["currentStage"] == "council"
    assert body["hasEmbeddings"] is True
    assert body["hasSkill"] is False
    assert body["councilInProgress"] is False


def test_status_council_in_progress_flag(client) -> None:
    c, registry, queue, _ = client
    _add_synced_source(registry, "demo")
    queue.record_session(
        session_id="cs_live",
        product_id="demo",
        topic="overview",
        proposal_id=None,
        deliberation=[],
        costs=[],
        started_at=datetime.now(UTC).isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
        status="running",
    )
    body = c.get("/products/demo/status").json()
    assert body["councilInProgress"] is True
    assert body["currentSessionId"] == "cs_live"
    assert body["currentStage"] == "council"


def test_status_stage_review_when_pending_proposal(client) -> None:
    c, registry, queue, _ = client
    _add_synced_source(registry, "demo")
    queue.enqueue(_pending_proposal(), session_id="cs_done", product_id="demo")
    body = c.get("/products/demo/status").json()
    assert body["currentStage"] == "review"


def test_status_stage_skill_when_approved_exists(client) -> None:
    c, registry, _, store = client
    _add_synced_source(registry, "demo")
    store.save(_demo_skill("demo"))
    body = c.get("/products/demo/status").json()
    assert body["currentStage"] == "skill"
    assert body["hasSkill"] is True
