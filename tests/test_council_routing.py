"""Conditional edge logic of the product skill-pack council graph."""

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
from nexus.council.state import JudgeResult, initial_state


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


def _route(missing_evidence: bool, callback_count: int) -> str:
    from nexus.council.agents.pack import should_callback

    return should_callback(
        {
            "judge_result": JudgeResult(
                passed=not missing_evidence,
                missing_evidence=missing_evidence,
            ),
            "callback_count": callback_count,
        }
    )


def test_missing_evidence_first_pass_routes_to_callback() -> None:
    assert _route(True, 0) == "targeted_callback"


def test_missing_evidence_after_callback_routes_to_finalizer() -> None:
    assert _route(True, 1) == "finalizer"


def test_no_missing_evidence_routes_to_finalizer() -> None:
    assert _route(False, 0) == "finalizer"


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
    assert state["callback_count"] == 0
    assert state["proposals"] == []


def test_build_graph_has_pack_nodes() -> None:
    """Smoke: graph has the bounded skill-pack council nodes."""
    from nexus.council.graph import CouncilHandles

    handles = CouncilHandles.__new__(CouncilHandles)
    handles.retrieval = None  # type: ignore[assignment]
    handles.chat_drafter = None  # type: ignore[assignment]
    handles.chat_critic = None  # type: ignore[assignment]
    handles.chat_reviser = None  # type: ignore[assignment]

    graph = build_graph(_make_cfg(), handles)
    assert "planner" in graph.nodes
    assert "experts" in graph.nodes
    assert "synthesizer" in graph.nodes
    assert "repair" in graph.nodes
    assert "judge" in graph.nodes
    assert "targeted_callback" in graph.nodes
    assert "finalizer" in graph.nodes
