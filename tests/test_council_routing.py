"""Routing shape of the single product-skill council graph."""

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
    assert state["proposals"] == []


def test_build_graph_has_skill_nodes() -> None:
    """Smoke: graph has the bounded product-skill council nodes."""
    from nexus.council.graph import CouncilHandles

    handles = CouncilHandles.__new__(CouncilHandles)
    handles.retrieval = None  # type: ignore[assignment]
    handles.chat_drafter = None  # type: ignore[assignment]
    handles.chat_critic = None  # type: ignore[assignment]
    handles.chat_reviser = None  # type: ignore[assignment]
    handles.chat_architect = None  # type: ignore[assignment]
    handles.chat_domain_expert = None  # type: ignore[assignment]
    handles.chat_quality_expert = None  # type: ignore[assignment]

    graph = build_graph(_make_cfg(), handles)
    assert "planner" in graph.nodes
    assert "architect" in graph.nodes
    assert "domain_expert" in graph.nodes
    assert "quality_expert" in graph.nodes
    assert "synthesizer" in graph.nodes
    assert "repair" in graph.nodes
    assert "skill_eval" in graph.nodes
    assert "finalizer" in graph.nodes
    assert "experts" not in graph.nodes
    assert "judge" not in graph.nodes
    assert "targeted_callback" not in graph.nodes
