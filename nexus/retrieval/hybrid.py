"""Hybrid retrieval - dense + BM25 -> Reciprocal Rank Fusion (RRF).

Stages 1-3 of the spec §5 pipeline:
  Stage 1 — dense ANN against the appropriate named vector (dense_code/dense_text)
  Stage 2 — sparse BM25 against the same chunk corpus
  Stage 3 — RRF merge into a single ranked seed set

The fusion is pure rank-based (no score normalisation needed), which makes it
robust to dense/sparse score-scale differences.
"""

from __future__ import annotations

from dataclasses import dataclass

# Standard RRF constant; spec uses k=60 (Vespa & Elasticsearch defaults).
RRF_K = 60


@dataclass(frozen=True)
class Hit:
    id: str
    score: float
    payload: dict
    source: str  # "dense" | "bm25"


def rrf_merge(
    rankings: list[list[Hit]],
    *,
    k: int = RRF_K,
    top_k: int = 20,
) -> list[Hit]:
    """Merge multiple ranked lists via Reciprocal Rank Fusion.

    For each item appearing in any input ranking, sum 1/(k+rank) across rankings.
    Returns the top_k by fused score, with payload taken from the first ranking
    that contributed the item.
    """
    fused: dict[str, float] = {}
    payloads: dict[str, dict] = {}
    sources: dict[str, set[str]] = {}

    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            fused[hit.id] = fused.get(hit.id, 0.0) + 1.0 / (k + rank)
            if hit.id not in payloads:
                payloads[hit.id] = hit.payload
            sources.setdefault(hit.id, set()).add(hit.source)

    merged = [
        Hit(
            id=hid,
            score=fused[hid],
            payload=payloads[hid],
            source="+".join(sorted(sources[hid])),
        )
        for hid in fused
    ]
    merged.sort(key=lambda h: h.score, reverse=True)
    return merged[:top_k]


__all__ = ["RRF_K", "Hit", "rrf_merge"]
