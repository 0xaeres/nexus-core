"""Information-retrieval metrics for code eval.

Pure-function helpers - testable without any infra.
"""

from __future__ import annotations

import math


def precision_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    if k <= 0 or not retrieved:
        return 0.0
    top = retrieved[:k]
    return sum(1 for r in top if r in relevant) / k


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 1.0
    top = retrieved[:k]
    return sum(1 for r in top if r in relevant) / len(relevant)


def dcg(rels: list[float]) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(rels))


def ndcg_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Binary relevance nDCG@k.

    relevance(item) = 1 if item in relevant else 0
    """
    if not relevant:
        return 1.0 if not retrieved else 0.0
    top = retrieved[:k]
    rels = [1.0 if r in relevant else 0.0 for r in top]
    actual = dcg(rels)
    ideal_rels = [1.0] * min(len(relevant), k)
    ideal = dcg(ideal_rels)
    if ideal == 0:
        return 0.0
    return actual / ideal


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
