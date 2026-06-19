from __future__ import annotations

import pytest

from nexus.graph import rag
from nexus.graph.models import GraphEdge, GraphNode, GraphQueryResult, GraphRAGQuery
from nexus.graph.rag import answer_graph_rag
from nexus.llm.client import ChatResponse, TokenUsage
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


class FakeGraph:
    def __init__(self):
        self.resolved = []
        self.traversed = []
        self.file_node = _node(
            "file:prod:shared/auth.py",
            ["CodeFile"],
            name="auth.py",
            resource_uri="shared/auth.py",
        )
        self.api_node = _node(
            "api:prod:GET:/tokens",
            ["APIEndpoint"],
            name="GET /tokens",
            path="/tokens",
            resource_uri="api/routes.py",
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
        if mention in {"shared/auth.py", "file:prod:shared/auth.py"}:
            return GraphQueryResult(nodes=[self.file_node])
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
            nodes=[self.api_node],
            edges=[
                GraphEdge(
                    product_id="prod",
                    stable_id="edge:1",
                    type="EXPOSES",
                    from_id=seed_ids[0],
                    to_id=self.api_node.stable_id,
                    source_refs=[],
                    last_seen="2026-01-01T00:00:00+00:00",
                )
            ],
        )

    async def aclose(self) -> None:
        pass


class FakeChat:
    def __init__(self):
        self.messages = []

    async def chat(self, messages, **kwargs):
        self.messages.append((messages, kwargs))
        return ChatResponse(
            content="Auth file exposes token route [C1]. Unknown runtime callers.",
            usage=TokenUsage(),
            model="fake",
        )


@pytest.mark.asyncio
async def test_graph_rag_answers_arbitrary_query_with_graph_and_citations(monkeypatch) -> None:
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
                    id="h1",
                    score=0.9,
                    source="rerank",
                    payload={
                        "resource_uri": "api/routes.py",
                        "start_line": 42,
                        "context_path": "tokens",
                        "content": "@router.get('/tokens')",
                        "graph_node_ids": ["api:prod:GET:/tokens"],
                    },
                )
            ],
            reranked=True,
            seed_count=1,
        )

    monkeypatch.setattr(rag, "retrieve", fake_retrieve)
    graph = FakeGraph()
    chat = FakeChat()

    answer = await answer_graph_rag(
        ctx=object(),
        graph_store=graph,
        chat=chat,
        product_id="prod",
        request=GraphRAGQuery(query="what depends on shared/auth.py?", top_k=4),
    )

    assert answer.answer == "Auth file exposes token route [C1]. Unknown runtime callers."
    assert answer.citations[0].id == "C1"
    assert answer.citations[0].anchor == "api/routes.py:42"
    assert answer.resolved_entities[0].resource_uri == "shared/auth.py"
    assert answer.graph_paths[0].edge_ids == ["edge:1"]
    assert answer.confidence > 0.6
    assert answer.graph_used is True
    assert seen["mode"] == "auto"
    assert seen["query"] == "what depends on shared/auth.py?"
    assert graph.traversed[0][3] == 3
    assert seen["graph_node_ids"] == [
        "file:prod:shared/auth.py",
        "api:prod:GET:/tokens",
    ]
    assert "graph_edges" in chat.messages[0][0][1]["content"]


@pytest.mark.asyncio
async def test_graph_rag_uses_qdrant_hits_as_graph_seeds_when_mentions_do_not_resolve(
    monkeypatch,
) -> None:
    calls = []

    async def fake_retrieve(*, ctx, product_id, query, top_k, mode, graph_node_ids=None):
        calls.append(query)
        return RetrievalResult(
            hits=[
                Hit(
                    id="h1",
                    score=0.8,
                    source="dense",
                    payload={
                        "resource_uri": "shared/auth.py",
                        "start_line": 1,
                        "content": "auth",
                        "graph_node_ids": ["file:prod:shared/auth.py"],
                    },
                )
            ],
            reranked=False,
            seed_count=1,
        )

    monkeypatch.setattr(rag, "retrieve", fake_retrieve)
    graph = FakeGraph()

    answer = await answer_graph_rag(
        ctx=object(),
        graph_store=graph,
        chat=None,
        product_id="prod",
        request=GraphRAGQuery(query="explain login flow"),
    )

    assert len(calls) == 2
    assert answer.graph_used is True
    assert answer.resolved_entities[0].stable_id == "file:prod:shared/auth.py"


@pytest.mark.asyncio
async def test_graph_rag_returns_clarification_for_ambiguous_entities(monkeypatch) -> None:
    class AmbiguousGraph(FakeGraph):
        async def resolve_entity(self, *, product_id: str, mention: str, limit: int = 10):
            return GraphQueryResult(
                nodes=[
                    _node(f"file:prod:{i}.py", ["CodeFile"], name=f"{i}.py", resource_uri=f"{i}.py")
                    for i in range(4)
                ]
            )

    async def fail_retrieve(**kwargs):
        raise AssertionError("ambiguous graph should not retrieve")

    monkeypatch.setattr(rag, "retrieve", fail_retrieve)

    answer = await answer_graph_rag(
        ctx=object(),
        graph_store=AmbiguousGraph(),
        chat=None,
        product_id="prod",
        request=GraphRAGQuery(query="AuthService"),
    )

    assert answer.needs_clarification is True
    assert len(answer.clarification_options) == 4


@pytest.mark.asyncio
async def test_graph_rag_broad_flow_question_retrieves_before_clarifying(monkeypatch) -> None:
    class BroadGraph(FakeGraph):
        async def resolve_entity(self, *, product_id: str, mention: str, limit: int = 10):
            self.resolved.append((product_id, mention, limit))
            return GraphQueryResult(
                nodes=[
                    _node(
                        f"file:prod:{i}.py",
                        ["CodeFile"],
                        name=f"{i}.py",
                        resource_uri=f"{i}.py",
                    )
                    for i in range(4)
                ]
            )

    calls = []

    async def fake_retrieve(*, ctx, product_id, query, top_k, mode, graph_node_ids=None):
        calls.append((query, graph_node_ids))
        return RetrievalResult(
            hits=[
                Hit(
                    id="h1",
                    score=0.8,
                    source="dense",
                    payload={
                        "resource_uri": "nexus/retrieval/pipeline.py",
                        "start_line": 1,
                        "content": "Retrieval pipeline: dense + sparse -> RRF merge -> rerank.",
                        "graph_node_ids": [],
                    },
                )
            ],
            reranked=False,
            seed_count=1,
        )

    monkeypatch.setattr(rag, "retrieve", fake_retrieve)
    graph = BroadGraph()

    answer = await answer_graph_rag(
        ctx=object(),
        graph_store=graph,
        chat=None,
        product_id="prod",
        request=GraphRAGQuery(
            query="can you please explain how the retrieval flow works like, in detail?"
        ),
    )

    assert answer.needs_clarification is False
    assert answer.citations[0].anchor == "nexus/retrieval/pipeline.py:1"
    assert graph.resolved == [("prod", "nexus/retrieval/pipeline.py", 3)]
    assert calls[0][0] == "can you please explain how the retrieval flow works like, in detail?"


@pytest.mark.asyncio
async def test_graph_rag_requires_graph_store() -> None:
    with pytest.raises(AttributeError):
        await answer_graph_rag(
            ctx=object(),
            graph_store=None,  # type: ignore[arg-type]
            chat=None,
            product_id="prod",
            request=GraphRAGQuery(query="anything"),
        )
