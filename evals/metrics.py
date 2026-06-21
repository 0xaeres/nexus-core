"""Shared deterministic metric helpers for Nexus evals."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence


def precision_at_k[T](retrieved: Sequence[T], relevant: set[T], k: int) -> float:
    if k <= 0 or not retrieved:
        return 0.0
    top = retrieved[:k]
    return sum(1 for item in top if item in relevant) / k


def recall_at_k[T](retrieved: Sequence[T], relevant: set[T], k: int) -> float:
    if not relevant:
        return 1.0
    top = retrieved[:k]
    return sum(1 for item in top if item in relevant) / len(relevant)


def first_match_rank[T, E](
    retrieved: Sequence[T],
    expected: Sequence[E],
    matcher: Callable[[T, Sequence[E]], bool],
) -> int | None:
    for index, item in enumerate(retrieved, start=1):
        if matcher(item, expected):
            return index
    return None


def reciprocal_rank(rank: int | None) -> float:
    return (1.0 / rank) if rank else 0.0


def mrr(ranks: Sequence[int | None]) -> float:
    return mean([reciprocal_rank(rank) for rank in ranks])


def dcg(rels: Sequence[float]) -> float:
    return sum(rel / math.log2(index + 2) for index, rel in enumerate(rels))


def ndcg_at_k[T](retrieved: Sequence[T], relevant: set[T], k: int) -> float:
    """Binary relevance nDCG@k."""
    if not relevant:
        return 1.0 if not retrieved else 0.0
    top = retrieved[:k]
    rels = [1.0 if item in relevant else 0.0 for item in top]
    actual = dcg(rels)
    ideal = dcg([1.0] * min(len(relevant), k))
    if ideal == 0:
        return 0.0
    return actual / ideal


def ndcg_by_relevance(relevance: Sequence[bool], k: int, relevant_count: int) -> float:
    if relevant_count <= 0:
        return 1.0 if not any(relevance[:k]) else 0.0
    rels = [1.0 if item else 0.0 for item in relevance[:k]]
    actual = dcg(rels)
    ideal = dcg([1.0] * min(relevant_count, k))
    if ideal == 0:
        return 0.0
    return actual / ideal


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
