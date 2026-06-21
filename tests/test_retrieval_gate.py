from __future__ import annotations

from types import SimpleNamespace

from nexus.config import ModelCfg, ModelsCfg, NexusConfig
from nexus.council.agents.skill import _no_evidence_error
from nexus.retrieval.hybrid import Hit
from nexus.retrieval.pipeline import _apply_quality_gate


def _hit(score: float) -> Hit:
    return Hit(id=str(score), score=score, payload={}, source="rerank")


def test_quality_gate_reports_filtered_hits_and_best_score() -> None:
    kept, filtered, best = _apply_quality_gate([_hit(0.1), _hit(0.2)], gate=0.3)

    assert kept == []
    assert filtered == 2
    assert best == 0.2


def test_drafter_empty_evidence_message_explains_quality_gate() -> None:
    m = ModelCfg(provider="test", model="test")
    config = NexusConfig(
        models=ModelsCfg(council=m, light=m, embedding=m, reranker=m),
        ingestion={"quality_gate_threshold": 0.3},
    )
    result = SimpleNamespace(
        seed_count=20,
        filtered_by_gate=20,
        best_score_before_gate=0.00000186,
    )

    err = _no_evidence_error(result, config)
    msg = err.detail

    assert "quality_gate_threshold=0.3" in msg
    assert "best_score=1.86e-06" in msg
    assert "Council stopped before planning" in err.user_message
