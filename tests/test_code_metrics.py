from evals.code_metrics import (
    dcg,
    mean,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)


def test_precision_at_k_basic() -> None:
    assert precision_at_k(["a", "b", "c", "d"], {"a", "c"}, 4) == 0.5
    assert precision_at_k(["a", "b", "c", "d"], {"a", "c"}, 2) == 0.5
    assert precision_at_k(["a", "b", "c", "d"], {"a"}, 1) == 1.0


def test_recall_at_k_basic() -> None:
    assert recall_at_k(["a", "b"], {"a", "b", "c"}, 2) == 2 / 3
    assert recall_at_k(["a", "b", "c"], {"a", "b", "c"}, 3) == 1.0
    assert recall_at_k(["x"], {"y"}, 1) == 0.0


def test_recall_with_no_relevant_returns_one() -> None:
    assert recall_at_k(["a", "b"], set(), 2) == 1.0


def test_ndcg_perfect_ordering() -> None:
    # All relevant at the top
    assert ndcg_at_k(["a", "b", "c"], {"a", "b", "c"}, 3) == 1.0


def test_ndcg_worse_ordering_lower_score() -> None:
    perfect = ndcg_at_k(["rel1", "rel2", "junk"], {"rel1", "rel2"}, 3)
    worse = ndcg_at_k(["junk", "rel1", "rel2"], {"rel1", "rel2"}, 3)
    assert worse < perfect


def test_ndcg_no_relevant_retrieved_is_zero() -> None:
    assert ndcg_at_k(["x", "y", "z"], {"a", "b"}, 3) == 0.0


def test_dcg_monotone() -> None:
    assert dcg([1.0, 1.0, 1.0]) > dcg([1.0, 1.0, 0.0])
    assert dcg([1.0, 0.0]) > dcg([0.0, 1.0])


def test_mean_handles_empty() -> None:
    assert mean([]) == 0.0
    assert mean([1.0, 2.0, 3.0]) == 2.0
