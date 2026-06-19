from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from nexus.config import ModelCfg, NexusConfig
from nexus.council import graph as council_graph
from nexus.council import runner
from nexus.council.errors import CouncilAgentError, CouncilNoEvidence
from nexus.council.queue import ProposalQueue


def _config(tmp_path: Path) -> NexusConfig:
    return NexusConfig(
        models={
            "council": {"provider": "deepinfra", "model": "shared"},
            "drafter": {"provider": "deepinfra", "model": "draft-model"},
            "critic": {"provider": "deepinfra", "model": "critic-model"},
            "reviser": {"provider": "deepinfra", "model": "revise-model"},
            "light": {"provider": "deepinfra", "model": "light"},
            "embedding": {"provider": "jina-local", "model": "j", "url": "http://embed"},
            "reranker": {"provider": "jina-local", "model": "r", "url": "http://rerank"},
        },
        storage={
            "proposal_queue": tmp_path / "proposals.db",
            "council_checkpoint": tmp_path / "council.sqlite",
        },
    )


class FakeRetrieval:
    async def aclose(self) -> None:
        pass


class FakeChat:
    def __init__(self, model: str, role: str):
        self.model = model
        self.role = role

    async def aclose(self) -> None:
        pass


class FakeGraphStore:
    async def ensure_schema(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_council_handles_use_role_specific_models(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, str] = {}

    def fake_from_config(_config):
        return FakeRetrieval()

    def fake_from_cfg(cfg: ModelCfg, *, role: str, token_sink=None):
        seen[role] = cfg.model
        return FakeChat(cfg.model, role)

    monkeypatch.setattr(council_graph.RetrievalContext, "from_config", fake_from_config)
    monkeypatch.setattr(council_graph.ChatClient, "from_cfg", fake_from_cfg)
    monkeypatch.setattr(council_graph, "create_graph_store", lambda _config: FakeGraphStore())

    async with council_graph.council_handles(_config(tmp_path)):
        pass

    assert seen == {
        "drafter": "draft-model",
        "critic": "critic-model",
        "reviser": "revise-model",
        "architect": "critic-model",
        "domain_expert": "critic-model",
        "quality_expert": "critic-model",
        "synthesizer": "draft-model",
    }


@pytest.mark.asyncio
async def test_council_agent_failure_records_failed_session_without_proposal(
    tmp_path: Path, monkeypatch
) -> None:
    queue = ProposalQueue(tmp_path / "queue.db")

    @asynccontextmanager
    async def fake_context(*_args, **_kwargs):
        yield object()

    class FailingCompiled:
        async def astream(self, *_args, **_kwargs):
            raise RuntimeError("critic exploded")
            yield  # pragma: no cover

    class FailingGraph:
        def compile(self, *_args, **_kwargs):
            return FailingCompiled()

    monkeypatch.setattr(runner, "council_handles", fake_context)
    monkeypatch.setattr(runner, "open_checkpointer", fake_context)
    monkeypatch.setattr(runner, "build_graph", lambda *_args, **_kwargs: FailingGraph())

    await runner._run_session(
        config=_config(tmp_path),
        queue=queue,
        session_id="cs_fail",
        product_id="p",
        topic="topic",
    )

    session = queue.get_session("cs_fail")
    assert session is not None
    assert session["status"] == "failed"
    assert session["proposal_id"] is None
    assert "critic exploded" in session["deliberation"][-1]["body"]
    assert queue.list(status="pending", product_id="p") == []


@pytest.mark.asyncio
async def test_council_no_evidence_stops_without_crash(tmp_path: Path, monkeypatch) -> None:
    queue = ProposalQueue(tmp_path / "queue.db")

    @asynccontextmanager
    async def fake_context(*_args, **_kwargs):
        yield object()

    class StoppedCompiled:
        async def astream(self, *_args, **_kwargs):
            raise CouncilAgentError(
                "drafter",
                CouncilNoEvidence(
                    user_message="Council stopped before drafting.",
                    detail="quality_gate_threshold=0.3 filtered every reranked hit",
                ),
            )
            yield  # pragma: no cover

    class StoppedGraph:
        def compile(self, *_args, **_kwargs):
            return StoppedCompiled()

    monkeypatch.setattr(runner, "council_handles", fake_context)
    monkeypatch.setattr(runner, "open_checkpointer", fake_context)
    monkeypatch.setattr(runner, "build_graph", lambda *_args, **_kwargs: StoppedGraph())

    await runner._run_session(
        config=_config(tmp_path),
        queue=queue,
        session_id="cs_stop",
        product_id="p",
        topic="topic",
    )

    session = queue.get_session("cs_stop")
    assert session is not None
    assert session["status"] == "stopped"
    assert session["proposal_id"] is None
    assert session["deliberation"][-1]["body"] == "Council stopped before drafting."
    assert queue.list(status="pending", product_id="p") == []
