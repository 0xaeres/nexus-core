from nexus.retrieval.hybrid import Hit, rrf_merge


def _h(id_: str, source: str = "dense") -> Hit:
    return Hit(id=id_, score=0.0, payload={"id": id_}, source=source)


def test_rrf_merges_disjoint_rankings() -> None:
    dense = [_h("a"), _h("b"), _h("c")]
    sparse = [_h("d", "bm25"), _h("e", "bm25"), _h("f", "bm25")]
    merged = rrf_merge([dense, sparse], k=60, top_k=10)
    ids = [h.id for h in merged]
    # All six should appear, and the first item of each ranking ties at rank 1.
    assert set(ids) == {"a", "b", "c", "d", "e", "f"}
    # Equal-rank items have the same RRF score
    assert abs(merged[0].score - merged[1].score) < 1e-9


def test_rrf_boosts_items_in_both_rankings() -> None:
    dense = [_h("shared"), _h("dense_only"), _h("c")]
    sparse = [_h("shared", "bm25"), _h("sparse_only", "bm25"), _h("c", "bm25")]
    merged = rrf_merge([dense, sparse], k=60, top_k=10)
    # 'shared' appears at rank 1 in both → highest fused score
    assert merged[0].id == "shared"
    # 'shared' source includes both contributors
    assert "dense" in merged[0].source and "bm25" in merged[0].source


def test_rrf_respects_top_k() -> None:
    long = [_h(f"x{i}") for i in range(100)]
    merged = rrf_merge([long], top_k=5)
    assert len(merged) == 5
    assert [h.id for h in merged] == [f"x{i}" for i in range(5)]
