from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from nexus.config import NexusConfig
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.retrieval.repomap import repomap_path_for
from nexus.skills.models import AppliesTo, Citation, Provenance, Skill, SkillProposal
from nexus.skills.store import SkillStore
from nexus.tools import delete_product as delete_product_module
from nexus.tools.delete_product import delete_product


def _config(tmp_path: Path) -> NexusConfig:
    return NexusConfig(
        models={
            "council": {"provider": "test", "model": "test"},
            "light": {"provider": "test", "model": "test"},
            "embedding": {"provider": "test", "model": "test", "url": "http://embed"},
            "reranker": {"provider": "test", "model": "test", "url": "http://rerank"},
        },
        hierarchy_root=tmp_path / "skills",
        storage={
            "proposal_queue": tmp_path / "proposals.db",
            "council_checkpoint": tmp_path / "council.sqlite",
        },
    )


def test_delete_product_dry_run_then_removes_local_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    registry = Registry(tmp_path / "registry.db")
    queue = ProposalQueue(cfg.storage.proposal_queue)
    store = SkillStore(cfg.hierarchy_root)
    now = datetime.now(UTC).isoformat()

    registry.upsert_product({
        "id": "demo",
        "name": "Demo",
        "tagline": "",
        "owner": {},
        "onboardedAt": now,
    })
    registry.upsert_source({
        "product": "demo",
        "name": "local",
        "type": "filesystem",
        "status": "connected",
        "config": {"root": str(tmp_path)},
    })
    registry.upsert_resource_manifest({
        "product": "demo",
        "sourceKey": "local",
        "resourceUri": "a.py",
        "contentHash": "h",
        "lastSeenSync": now,
        "indexedAt": now,
        "chunkIds": ["chunk-1"],
    })
    run_id = registry.start_sync_run("demo", "local", now)
    registry.finish_sync_run(
        run_id,
        finished_at=now,
        added=1,
        updated=0,
        removed=0,
        unchanged=0,
        status="done",
    )
    proposal = SkillProposal(
        id="prop_1",
        name="Demo skill",
        body="body",
        citations=[Citation(file="a.py", line=1)],
        confidence=0.7,
        created_at=now,
    )
    queue.enqueue(proposal, session_id="cs_1", product_id="demo")
    queue.record_session(
        session_id="cs_1",
        product_id="demo",
        topic="overview",
        proposal_id="prop_1",
        deliberation=[],
        costs=[],
        started_at=now,
        completed_at=now,
    )
    store.save(
        Skill(
            name="demo-overview",
            product="demo",
            confidence=0.8,
            applies_to=AppliesTo(),
            provenance=Provenance(validated_by="jl", validated_at=now),
            body="# Demo\n",
        )
    )
    repomap_path = repomap_path_for(tmp_path, "demo")
    repomap_path.parent.mkdir(parents=True)
    repomap_path.write_text("{}", encoding="utf-8")
    with sqlite3.connect(cfg.storage.council_checkpoint) as conn:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)")
        conn.execute("INSERT INTO checkpoints VALUES (?, ?)", ("cs_1", "ckpt_1"))

    class FakeGraphStore:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete_product(self, *, product_id: str) -> int:
            self.deleted.append(product_id)
            return 1

        async def aclose(self) -> None:
            pass

    graph = FakeGraphStore()
    monkeypatch.setattr(
        delete_product_module,
        "create_graph_store",
        lambda config: graph,
    )

    dry = asyncio.run(
        delete_product(product_id="demo", config=cfg, dry_run=True, skip_qdrant=True)
    )

    assert dry.registry["products"] == 1
    assert dry.registry["source_resources"] == 1
    assert dry.queue["proposals"] == 1
    assert dry.skills == 1
    assert dry.repomap_deleted is True
    assert dry.checkpoints == 1
    assert registry.get_product("demo") is not None

    report = asyncio.run(
        delete_product(product_id="demo", config=cfg, dry_run=False, skip_qdrant=True)
    )

    assert report.graph_deleted is True
    assert graph.deleted == ["demo"]
    assert registry.get_product("demo") is None
    assert registry.list_sources("demo") == []
    assert queue.list(product_id="demo") == []
    assert queue.list_sessions(product_id="demo") == []
    assert [s for s in store.iter_skills() if s.product == "demo"] == []
    assert not repomap_path.exists()
    with sqlite3.connect(cfg.storage.council_checkpoint) as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
