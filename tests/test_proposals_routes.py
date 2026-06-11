from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from nexus.api.app import app
from nexus.api.deps import get_config_dep, get_proposal_queue, get_registry
from nexus.api.routes import proposals
from nexus.config import NexusConfig
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.skills.models import Citation, SkillProposal


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


def _proposal() -> SkillProposal:
    return SkillProposal(
        id="prop_demo",
        name="Demo skill",
        body="body",
        citations=[Citation(file="a.py", line=1)],
        confidence=0.6,
        created_at=datetime.now(UTC).isoformat(),
    )


def test_reject_accepts_json_body(tmp_path: Path) -> None:
    queue = ProposalQueue(tmp_path / "proposals.db")
    registry = Registry(tmp_path / "registry.db")
    proposal = _proposal()
    queue.enqueue(proposal, session_id="cs_demo", product_id="demo")
    app.dependency_overrides[get_proposal_queue] = lambda: queue
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        client = TestClient(app)
        res = client.post(
            f"/proposals/{proposal.id}/reject",
            json={"reason": "Claims need stronger citations.", "actor": "reviewer@x"},
        )
    finally:
        app.dependency_overrides.pop(get_proposal_queue, None)
        app.dependency_overrides.pop(get_registry, None)

    assert res.status_code == 200
    assert res.json() == {
        "ok": True,
        "reason": "Claims need stronger citations.",
        "category": None,
    }
    row = queue.get(proposal.id)
    assert row is not None
    assert row["status"] == "rejected"
    assert row["approved_by"] is None


def test_reject_requires_reason(tmp_path: Path) -> None:
    queue = ProposalQueue(tmp_path / "proposals.db")
    registry = Registry(tmp_path / "registry.db")
    proposal = _proposal()
    queue.enqueue(proposal, session_id="cs_demo", product_id="demo")
    app.dependency_overrides[get_proposal_queue] = lambda: queue
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        client = TestClient(app)
        res = client.post(f"/proposals/{proposal.id}/reject", json={})
    finally:
        app.dependency_overrides.pop(get_proposal_queue, None)
        app.dependency_overrides.pop(get_registry, None)

    assert res.status_code == 422
    row = queue.get(proposal.id)
    assert row is not None
    assert row["status"] == "pending"


def test_revise_starts_council_with_feedback(tmp_path: Path, monkeypatch) -> None:
    queue = ProposalQueue(tmp_path / "proposals.db")
    registry = Registry(tmp_path / "registry.db")
    proposal = _proposal()
    queue.enqueue(proposal, session_id="cs_demo", product_id="demo")
    seen: dict[str, str] = {}

    async def fake_kick_off(**kwargs):
        seen["product_id"] = kwargs["product_id"]
        seen["topic"] = kwargs["topic"]
        return "cs_revision"

    monkeypatch.setattr(proposals, "kick_off", fake_kick_off)
    app.dependency_overrides[get_proposal_queue] = lambda: queue
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_config_dep] = lambda: _config(tmp_path)
    try:
        client = TestClient(app)
        res = client.post(
            f"/proposals/{proposal.id}/revise",
            json={
                "summary": "Tighten citations and remove stale guidance.",
                "comments": [{"line": 2, "body": "This claim needs an anchor."}],
            },
        )
    finally:
        app.dependency_overrides.pop(get_proposal_queue, None)
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_config_dep, None)

    assert res.status_code == 200
    assert res.json() == {"session_id": "cs_revision", "status": "running"}
    assert seen["product_id"] == "demo"
    assert "Tighten citations" in seen["topic"]
    assert "line 2" in seen["topic"]
    assert "Previous draft" in seen["topic"]
    row = queue.get(proposal.id)
    assert row is not None
    assert row["status"] == "revision_requested"
