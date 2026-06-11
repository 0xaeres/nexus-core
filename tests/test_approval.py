"""Approval flow: queue row -> .skill.md on disk -> queue status update.

We mock the embedder/indexer side (no Qdrant running) by pointing the config at
a non-existent host - the approval function tolerates ingest failures and still
writes the skill file + flips status.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from git import Repo

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
from nexus.skills.approval import _wrap_markdown_body, approve_proposal
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
        description="Use for demo approval tests.",
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
    )
    return p


def test_approve_writes_skill_file_and_flips_status(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    Repo.init(cfg.hierarchy_root)
    queue = ProposalQueue(cfg.storage.proposal_queue)
    p = _seed_proposal(queue)

    result = asyncio.run(
        approve_proposal(
            proposal_id=p.id, actor="reviewer@example", config=cfg, queue=queue
        )
    )
    assert result["ok"] is True
    # Skill file landed on disk under Agent Skills layout: <product>/<name>/SKILL.md.
    expected = tmp_path / "skills" / "forge" / "demo-skill" / "SKILL.md"
    assert expected.exists()
    contents = expected.read_text(encoding="utf-8")
    assert "description: Use for demo approval tests." in contents
    assert "nexus_product: forge" in contents
    assert "## Rules" in contents
    assert "[file: a.py:1]" in contents
    # Queue row flipped to approved + actor stamped
    row = queue.get(p.id)
    assert row is not None
    assert row["status"] == "approved"
    assert row["approved_by"] == "reviewer@example"
    assert row["git_committed"] == 1
    assert row["skill_index_status"] == "pending"


def test_approve_product_skill_writes_flat_file_and_reloads(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    Repo.init(cfg.hierarchy_root)
    queue = ProposalQueue(cfg.storage.proposal_queue)
    proposal = SkillProposal(
        id="prop_product_skill",
        name="forge-skill",
        description="Use for product orientation and grounded development.",
        tier="product_master",
        body="# forge-skill\n\n## Use This Skill When\n\nUse for grounded work.\n",
        citations=[Citation(file="a.py", line=1, excerpt="x")],
        confidence=0.6,
        status="pending",
        created_at="2026-05-19T00:00:00Z",
    )
    queue.enqueue(proposal, session_id="cs_seed", product_id="forge")

    result = asyncio.run(
        approve_proposal(
            proposal_id=proposal.id,
            actor="reviewer@example",
            config=cfg,
            queue=queue,
        )
    )

    expected = tmp_path / "skills" / "forge" / "forge-skill.md"
    assert result["ok"] is True
    assert expected.exists()
    from nexus.skills.store import SkillStore

    loaded = SkillStore(tmp_path / "skills").load("forge/forge-skill.md")
    assert loaded.name == "forge-skill"
    assert loaded.product == "forge"


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
    Repo.init(cfg.hierarchy_root)
    queue = ProposalQueue(cfg.storage.proposal_queue)
    p = _seed_proposal(queue)
    asyncio.run(approve_proposal(proposal_id=p.id, actor="me", config=cfg, queue=queue))
    second = asyncio.run(
        approve_proposal(proposal_id=p.id, actor="me", config=cfg, queue=queue)
    )
    assert second.get("skipped") == "already_approved"
    assert second.get("skill_id") == "forge/demo-skill"


def test_approve_fails_without_git_commit_and_keeps_pending(tmp_path: Path) -> None:
    from nexus.skills.approval import ApprovalError

    cfg = _make_cfg(tmp_path)
    queue = ProposalQueue(cfg.storage.proposal_queue)
    p = _seed_proposal(queue)

    with pytest.raises(ApprovalError):
        asyncio.run(
            approve_proposal(proposal_id=p.id, actor="me", config=cfg, queue=queue)
        )

    row = queue.get(p.id)
    assert row is not None
    assert row["status"] == "pending"
    assert not (Path(cfg.hierarchy_root) / "forge" / "demo-skill" / "SKILL.md").exists()


def test_approve_failed_push_keeps_pending_without_local_commit(tmp_path: Path) -> None:
    from nexus.skills.approval import ApprovalPublishError

    cfg = _make_cfg(tmp_path)
    repo = Repo.init(cfg.hierarchy_root)
    repo.create_remote("origin", str(tmp_path / "missing-remote.git"))
    queue = ProposalQueue(cfg.storage.proposal_queue)
    p = _seed_proposal(queue)

    with pytest.raises(ApprovalPublishError):
        asyncio.run(
            approve_proposal(proposal_id=p.id, actor="me", config=cfg, queue=queue)
        )

    row = queue.get(p.id)
    assert row is not None
    assert row["status"] == "pending"
    assert not (Path(cfg.hierarchy_root) / "forge" / "demo-skill" / "SKILL.md").exists()
    with pytest.raises(ValueError):
        list(repo.iter_commits())


def test_wrap_markdown_body_wraps_prose_but_preserves_code_fences() -> None:
    long_sentence = " ".join(["Nexus keeps generated skill prose readable"] * 8)
    body = (
        "# product-skill\n\n"
        f"{long_sentence}\n\n"
        "- " + " ".join(["List guidance stays readable"] * 8) + "\n\n"
        "```python\n"
        "x = '" + ("a" * 140) + "'\n"
        "```\n"
    )

    wrapped = _wrap_markdown_body(body, width=88)

    in_fence = False
    for line in wrapped.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        assert len(line) <= 88
    assert "x = '" + ("a" * 140) + "'" in wrapped
