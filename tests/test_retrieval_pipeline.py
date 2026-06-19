from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.retrieval import pipeline


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed_query(self, text: str, *, vector: str) -> list[float]:
        self.calls.append(vector)
        return [1.0] if vector == "dense_code" else [2.0]


@pytest.mark.asyncio
async def test_auto_retrieval_embeds_code_and_text_queries(monkeypatch) -> None:
    embedder = FakeEmbedder()
    ctx = SimpleNamespace(embedder=embedder)

    async def fake_hybrid_search(**kwargs):
        assert kwargs["query_vectors"] == {"code": [1.0], "text": [2.0]}
        return []

    monkeypatch.setattr(pipeline, "_hybrid_search", fake_hybrid_search)

    result = await pipeline.retrieve(
        ctx=ctx, product_id="p", query="how does auth work?", mode="auto"
    )

    assert result.hits == []
    assert embedder.calls == ["dense_code", "dense_text"]


@pytest.mark.asyncio
async def test_hybrid_search_includes_product_scoped_graph_node_hits() -> None:
    class FakeIndexer:
        async def search_dense(self, **kwargs):
            return []

        async def search_sparse(self, **kwargs):
            return []

        async def search_by_graph_nodes(self, **kwargs):
            assert kwargs["product_id"] == "p"
            assert kwargs["graph_node_ids"] == ["node:1"]
            assert kwargs["vector_kind"] in {"code", "text"}
            return [
                {
                    "id": f"hit-{kwargs['vector_kind']}",
                    "score": 1.0,
                    "payload": {
                        "product_id": "p",
                        "graph_node_ids": ["node:1"],
                    },
                }
            ]

    ctx = SimpleNamespace(indexer=FakeIndexer())

    hits = await pipeline._hybrid_search(
        ctx=ctx,
        product_id="p",
        query_vectors={"code": [1.0], "text": [2.0]},
        sparse_query="auth",
        vector_kinds=["code", "text"],
        graph_node_ids=["node:1"],
    )

    assert {hit.id for hit in hits} == {"hit-code", "hit-text"}
    assert all(hit.source == "graph" for hit in hits)
