from __future__ import annotations

from evals.metrics import first_match_rank, mrr, ndcg_at_k, ndcg_by_relevance


def test_first_match_rank_returns_one_indexed_rank() -> None:
    rank = first_match_rank(
        ["miss", "hit", "later"],
        ["hit"],
        lambda item, expected: item in expected,
    )
    assert rank == 2


def test_first_match_rank_returns_none_when_no_match() -> None:
    rank = first_match_rank(
        ["miss"],
        ["hit"],
        lambda item, expected: item in expected,
    )
    assert rank is None


def test_mrr_averages_reciprocal_ranks() -> None:
    assert mrr([1, 5, None, 2]) == (1.0 + 0.2 + 0.0 + 0.5) / 4
    assert mrr([]) == 0.0


def test_ndcg_by_relevance_uses_binary_positions() -> None:
    perfect = ndcg_by_relevance([True, True, False], k=3, relevant_count=2)
    worse = ndcg_by_relevance([False, True, True], k=3, relevant_count=2)
    assert perfect == 1.0
    assert worse < perfect


def test_ndcg_at_k_handles_no_relevant_items() -> None:
    assert ndcg_at_k([], set(), 10) == 1.0
    assert ndcg_at_k(["a"], set(), 10) == 0.0
