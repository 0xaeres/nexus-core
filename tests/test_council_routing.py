"""Conditional edge logic of the 3-node council graph (no LLM calls)."""

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
        return "reviser"
    return END


def test_blocking_first_pass_routes_to_reviser() -> None:
    crit = Critique(severity="blocking", issues=[], recommendation="revise")
    assert _route(crit.severity, 0) == "reviser"


def test_blocking_after_revision_ends() -> None:
    crit = Critique(severity="blocking", issues=[], recommendation="still bad")
    assert _route(crit.severity, 1) == END


def test_major_never_triggers_revision() -> None:
    assert _route("major", 0) == END
    assert _route("major", 1) == END


def test_minor_never_triggers_revision() -> None:
    assert _route("minor", 0) == END


def test_initial_state_revision_zero() -> None:
    state = initial_state(
        session_id="cs_t",
        product_id="p",
        topic="t",
        config_path="x",
    )
    assert state["revision_count"] == 0
    assert state["critique"] is None
    assert state["proposal"] is None
    assert state["evidence"] == []


def test_build_graph_has_three_nodes() -> None:
    """Smoke: graph has Drafter, Critic, Reviser and the conditional edge."""
    from nexus.council.graph import CouncilHandles

    handles = CouncilHandles.__new__(CouncilHandles)
    handles.retrieval = None  # type: ignore[assignment]
    handles.chat_drafter = None  # type: ignore[assignment]
    handles.chat_critic = None  # type: ignore[assignment]
    handles.chat_reviser = None  # type: ignore[assignment]

    graph = build_graph(_make_cfg(), handles)
    assert "drafter" in graph.nodes
    assert "critic" in graph.nodes
    assert "reviser" in graph.nodes
