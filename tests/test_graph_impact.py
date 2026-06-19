from __future__ import annotations

import pytest

from nexus.graph import impact
from nexus.graph.impact import build_change_impact, build_dependency_trace
from nexus.graph.models import GraphEdge, GraphNode, GraphQueryResult
from nexus.retrieval.hybrid import Hit
from nexus.retrieval.pipeline import RetrievalResult


def _node(stable_id: str, labels: list[str], **props) -> GraphNode:
    return GraphNode(
        product_id="prod",
        stable_id=stable_id,
        labels=labels,
        properties=props,
        source_refs=[],
        last_seen="2026-01-01T00:00:00+00:00",
    )


class FakeImpactGraph:
    def __init__(self):
        self.resolved = []
        self.traversed = []
        self.seed = _node(
            "file:prod:shared/auth.py",
            ["CodeFile"],
            name="auth.py",
            resource_uri="shared/auth.py",
        )
        self.api = _node(
            "api:prod:GET:/tokens",
            ["APIEndpoint"],
            name="GET /tokens",
            path="/tokens",
            resource_uri="api/routes.py",
        )
        self.test = _node(
            "symbol:prod:tests/test_auth.py:test_tokens:Function",
            ["Function", "Test"],
            name="test_tokens",
            resource_uri="tests/test_auth.py",
        )
        self.config = _node(
            "config:prod:TOKEN_SECRET",
            ["Config"],
            name="TOKEN_SECRET",
        )

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
        if mention in {"shared/auth.py", "auth"}:
            return GraphQueryResult(nodes=[self.seed])
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
        return GraphQueryResult(
            nodes=[self.api, self.test, self.config],
            edges=[
                GraphEdge(
                    product_id="prod",
                    stable_id="edge:api",
                    type="EXPOSES",
                    from_id=seed_ids[0],
                    to_id=self.api.stable_id,
                    source_refs=[],
                    last_seen="2026-01-01T00:00:00+00:00",
                ),
                GraphEdge(
                    product_id="prod",
                    stable_id="edge:test",
                    type="COVERS",
                    from_id=self.test.stable_id,
                    to_id=seed_ids[0],
                    source_refs=[],
                    last_seen="2026-01-01T00:00:00+00:00",
                ),
            ],
        )

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_change_impact_returns_affected_entities_checks_and_evidence(monkeypatch) -> None:
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
                    score=0.88,
                    source="rerank",
                    payload={
                        "resource_uri": "api/routes.py",
                        "start_line": 12,
                        "content": "@router.get('/tokens')",
                        "graph_node_ids": ["api:prod:GET:/tokens"],
                    },
                )
            ],
            reranked=True,
            seed_count=1,
        )

    monkeypatch.setattr(impact, "retrieve", fake_retrieve)
    graph = FakeImpactGraph()

    result = await build_change_impact(
        ctx=object(),
        graph_store=graph,
        product_id="prod",
        query="auth token change",
        changed_files=["shared/auth.py"],
    )

    categories = {entity.category for entity in result.affected_entities}
    assert result.graph_used is True
    assert {"api", "test", "runtime"} <= categories
    assert "Run affected tests." in result.required_checks
    assert "Verify affected API handlers and contracts." in result.required_checks
    assert result.evidence[0].anchor == "api/routes.py:12"
    assert result.confidence > 0.7
    assert seen["mode"] == "auto"
    assert "TOKEN_SECRET" in seen["query"]
    assert "api:prod:GET:/tokens" in seen["graph_node_ids"]
    assert graph.traversed[0][3] == 3


@pytest.mark.asyncio
async def test_dependency_trace_splits_upstream_and_downstream(monkeypatch) -> None:
    async def fake_retrieve(*, ctx, product_id, query, top_k, mode, graph_node_ids=None):
        return RetrievalResult(hits=[], reranked=False, seed_count=0)

    monkeypatch.setattr(impact, "retrieve", fake_retrieve)
    graph = FakeImpactGraph()

    result = await build_dependency_trace(
        ctx=object(),
        graph_store=graph,
        product_id="prod",
        query="auth dependencies",
        seeds=["shared/auth.py"],
    )

    assert [entity.name for entity in result.downstream] == ["GET /tokens"]
    assert [entity.name for entity in result.upstream] == ["test_tokens"]
    assert result.graph_used is True


@pytest.mark.asyncio
async def test_change_impact_requires_graph_store() -> None:
    with pytest.raises(AttributeError):
        await build_change_impact(
            ctx=object(),
            graph_store=None,  # type: ignore[arg-type]
            product_id="prod",
            query="change",
        )
