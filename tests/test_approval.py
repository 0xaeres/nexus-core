"""Approval flow: queue row -> .skill.md on disk -> queue status update.

We mock the embedder/indexer side (no Qdrant running) by pointing the config at
a non-existent host - the approval function tolerates ingest failures and still
writes the skill file + flips status.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus.config import (
    EnrichCfg,
    IngestionCfg,
    ModelCfg,
    ModelsCfg,
    NexusConfig,
    ServerCfg,
    StorageCfg,
    VectorStoreCfg,
)
from nexus.council.queue import ProposalQueue
from nexus.skills.approval import approve_proposal
from nexus.skills.models import Citation, SkillProposal


def _make_cfg(tmp_path: Path) -> NexusConfig:
    m = ModelCfg(provider="deepinfra", model="x")
    return NexusConfig(
        skills_repo="git@example:repo.git",
        hierarchy_root=tmp_path / "skills",
        connectors=[],
        vector_store=VectorStoreCfg(url="http://127.0.0.1:1"),  # dead port
        models=ModelsCfg(
            council=m,
            light=m,
            embedding=ModelCfg(
                provider="jina-local", model="j", url="http://127.0.0.1:1"
            ),
            reranker=ModelCfg(provider="jina-local", model="j", url="http://127.0.0.1:1"),
        ),
        ingestion=IngestionCfg(enrich_chunks=EnrichCfg()),
        server=ServerCfg(),
        storage=StorageCfg(
            proposal_queue=tmp_path / "queue.db",
            council_checkpoint=tmp_path / "council.sqlite",
        ),
    )


def _seed_proposal(queue: ProposalQueue) -> SkillProposal:
    p = SkillProposal(
        id="prop_seed",
        name="demo-skill",
        body="# Demo\n\n## Rules\n\n1. Cited [file: a.py:1].\n",
        citations=[Citation(file="a.py", line=1, excerpt="x")],
        confidence=0.6,
        status="pending",
        created_at="2026-05-19T00:00:00Z",
    )
    queue.enqueue(
        p,
        session_id="cs_seed",
        product_id="forge",
        skill_kind="product_domain",
    )
    return p


def test_approve_writes_skill_file_and_flips_status(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    queue = ProposalQueue(cfg.storage.proposal_queue)
    p = _seed_proposal(queue)

    result = asyncio.run(
        approve_proposal(
            proposal_id=p.id, actor="reviewer@example", config=cfg, queue=queue
        )
    )
    assert result["ok"] is True
    # Skill file landed on disk under hierarchy_root
    expected = tmp_path / "skills" / "L2_domain" / "demo-skill.skill.md"
    assert expected.exists()
    contents = expected.read_text(encoding="utf-8")
    assert "## Rules" in contents
    assert "[file: a.py:1]" in contents
    # Queue row flipped to approved + actor stamped
    row = queue.get(p.id)
    assert row is not None
    assert row["status"] == "approved"
    assert row["approved_by"] == "reviewer@example"


def test_approve_unknown_proposal_raises(tmp_path: Path) -> None:
    from nexus.skills.approval import ApprovalError

    cfg = _make_cfg(tmp_path)
    queue = ProposalQueue(cfg.storage.proposal_queue)
    with pytest.raises(ApprovalError):
        asyncio.run(
            approve_proposal(
                proposal_id="prop_missing", actor="x", config=cfg, queue=queue
            )
        )


def test_approve_twice_is_idempotent(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    queue = ProposalQueue(cfg.storage.proposal_queue)
    p = _seed_proposal(queue)
    asyncio.run(approve_proposal(proposal_id=p.id, actor="me", config=cfg, queue=queue))
    second = asyncio.run(
        approve_proposal(proposal_id=p.id, actor="me", config=cfg, queue=queue)
    )
    assert second.get("skipped") == "already_approved"
