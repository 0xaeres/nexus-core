from __future__ import annotations

import pytest

from nexus.graph import context
from nexus.graph.context import build_code_context_pack
from nexus.graph.models import GraphEdge, GraphNode, GraphQueryResult, SourceRef
from nexus.retrieval.hybrid import Hit
from nexus.retrieval.pipeline import RetrievalResult


def _node(stable_id: str, *, name: str, resource_uri: str = "app.py") -> GraphNode:
    return GraphNode(
        product_id="prod",
        stable_id=stable_id,
        labels=["Function"],
        properties={
            "name": name,
            "resource_uri": resource_uri,
            "start_line": 10,
            "end_line": 20,
        },
        source_refs=[
            SourceRef(
                product_id="prod",
                source_key="source",
                source_id="local:test",
                resource_uri=resource_uri,
                anchor=f"{resource_uri}:10",
            )
        ],
        last_seen="2026-01-01T00:00:00+00:00",
    )


class FakeGraphStore:
    def __init__(self):
        self.resolved = []
        self.traversed = []

    async def ensure_schema(self) -> None:
        pass

    async def upsert_resource_graph(self, extraction, *, previous_fact_ids=None):
        return extraction.fact_ids

    async def retire_resource_graph(self, *, product_id: str, fact_ids: list[str]):
        return len(fact_ids)

    async def delete_product(self, *, product_id: str):
        return 0

    async def resolve_entity(self, *, product_id: str, mention: str, limit: int = 10):
        self.resolved.append((product_id, mention, limit))
        if mention in {"app.py", "auth_token"}:
            return GraphQueryResult(nodes=[_node("symbol:prod:app.py:auth_token:Function", name="auth_token")])
        return GraphQueryResult()

    async def traverse(
        self,
        *,
        product_id: str,
        seed_ids: list[str],
        edge_types: list[str] | None = None,
        max_depth: int = 2,
        limit: int = 50,
    ):
        self.traversed.append((product_id, seed_ids, edge_types, max_depth, limit))
        edge = GraphEdge(
            product_id="prod",
            stable_id="edge:1",
            type="IMPORTS",
            from_id=seed_ids[0],
            to_id="module:prod:shared.auth",
            source_refs=[],
            last_seen="2026-01-01T00:00:00+00:00",
        )
        return GraphQueryResult(
            nodes=[_node("module:prod:shared.auth", name="shared.auth", resource_uri="shared/auth.py")],
            edges=[edge],
        )

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_code_context_pack_uses_graph_entities_to_bias_retrieval(monkeypatch) -> None:
    seen = {}

    async def fake_retrieve(*, ctx, product_id, query, top_k, mode, graph_node_ids=None):
        seen.update({
            "product_id": product_id,
            "query": query,
            "top_k": top_k,
            "mode": mode,
            "graph_node_ids": graph_node_ids,
        })
        return RetrievalResult(
            hits=[
                Hit(
                    id="hit-1",
                    score=0.9,
                    source="rerank",
                    payload={
                        "resource_uri": "app.py",
                        "start_line": 10,
                        "context_path": "auth_token",
                        "content": "def auth_token(): ...",
                        "graph_node_ids": ["symbol:prod:app.py:auth_token:Function"],
                    },
                )
            ],
            reranked=True,
            seed_count=1,
        )

    monkeypatch.setattr(context, "retrieve", fake_retrieve)
    graph = FakeGraphStore()

    pack = await build_code_context_pack(
        ctx=object(),
        graph_store=graph,
        product_id="prod",
        query="auth_token",
        current_file="app.py",
        top_k=3,
    )

    assert pack.graph_used is True
    assert pack.graph_available is True
    assert pack.resolved_entities[0].name == "auth_token"
    assert pack.neighbor_entities[0].resource_uri == "shared/auth.py"
    assert pack.evidence[0].anchor == "app.py:10"
    assert seen["product_id"] == "prod"
    assert seen["mode"] == "code"
    assert "shared.auth" in seen["query"]
    assert seen["graph_node_ids"] == [
        "symbol:prod:app.py:auth_token:Function",
        "module:prod:shared.auth",
    ]
    assert graph.traversed[0][0] == "prod"


@pytest.mark.asyncio
async def test_code_context_pack_requires_graph_store() -> None:
    with pytest.raises(AttributeError):
        await build_code_context_pack(
            ctx=object(),
            graph_store=None,  # type: ignore[arg-type]
            product_id="prod",
            query="anything",
        )
