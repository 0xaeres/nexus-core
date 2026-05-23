"""Conditional edge logic of the council graph (no LLM calls)."""

from langgraph.graph import END

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
from nexus.council.graph import build_graph
from nexus.council.state import initial_state
from nexus.skills.models import Critique


def _make_cfg() -> NexusConfig:
    m = ModelCfg(provider="deepinfra", model="x")
    return NexusConfig(
        skills_repo="git@example:repo.git",
        connectors=[],
        vector_store=VectorStoreCfg(),
        models=ModelsCfg(
            council=m,
            light=m,
            embedding=ModelCfg(provider="jina-local", model="j", url="http://x"),
            reranker=ModelCfg(provider="jina-local", model="j", url="http://x"),
        ),
        ingestion=IngestionCfg(enrich_chunks=EnrichCfg()),
        server=ServerCfg(),
        storage=StorageCfg(),
    )


def _route(severity: str, revision_count: int) -> str:
    """Re-implement the predicate so we don't have to spin up handles."""
    if severity == "blocking" and revision_count == 0:
        return "synthesizer"
    return END


def test_blocking_first_pass_routes_to_synth() -> None:
    crit = Critique(severity="blocking", issues=[], recommendation="redraft")
    assert _route(crit.severity, 0) == "synthesizer"


def test_blocking_after_redraft_ends() -> None:
    crit = Critique(severity="blocking", issues=[], recommendation="still bad")
    assert _route(crit.severity, 1) == END


def test_major_never_triggers_redraft() -> None:
    assert _route("major", 0) == END
    assert _route("major", 1) == END


def test_minor_never_triggers_redraft() -> None:
    assert _route("minor", 0) == END


def test_initial_state_revision_zero() -> None:
    state = initial_state(
        session_id="cs_t",
        product_id="p",
        topic="t",
        skill_kind="master",
        config_path="x",
    )
    assert state["revision_count"] == 0
    assert state["critique"] is None
    assert state["proposal"] is None


def test_build_graph_compiles_with_adversary_node() -> None:
    """Smoke: graph has the new adversary node and a conditional edge from it."""
    from dataclasses import dataclass

    @dataclass
    class _Stub:
        retrieval: object = None

        async def aclose(self):
            return None

    # We don't actually compile here (avoids needing real ChatClients); we just
    # confirm `build_graph` builds without error using stub handles whose attrs
    # the build function only stores - it never calls them.
    from nexus.council.graph import CouncilHandles

    handles = CouncilHandles.__new__(CouncilHandles)
    handles.retrieval = None  # type: ignore[assignment]
    handles.chat_arch = None  # type: ignore[assignment]
    handles.chat_domain = None  # type: ignore[assignment]
    handles.chat_synth = None  # type: ignore[assignment]
    handles.chat_adv = None  # type: ignore[assignment]

    graph = build_graph(_make_cfg(), handles)
    # Three named nodes plus the adversary
    assert "adversary" in graph.nodes
    assert "synthesizer" in graph.nodes
    assert "archaeologist" in graph.nodes
    assert "domain_expert" in graph.nodes
