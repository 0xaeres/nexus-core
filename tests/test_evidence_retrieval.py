from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from anvay.graph.models import GraphNode, GraphQueryResult
from anvay.ingest.models import ResourceRef
from anvay.ingest.summaries import (
    graph_community_summary_chunk,
    graph_summary_chunk,
    is_community_summary_chunk,
    is_summary_chunk,
)
from anvay.retrieval import evidence
from anvay.retrieval.evidence import (
    EvidenceCandidate,
    merge_candidates,
    retrieve_evidence,
    understand_query,
)
from anvay.retrieval.hybrid import Hit
from anvay.retrieval.pipeline import RetrievalResult
from anvay.retrieval.repomap import RepoMap, Symbol, repomap_path_for, save_repo_map
from anvay.skills.models import AppliesTo, Provenance, Skill


def test_understand_query_classifies_global_chunking_strategy() -> None:
    u = understand_query("explain our chunking strategy")
    assert u.shape == "global"
    assert "overview" in u.facets
    assert "implementation" in u.facets


def test_understand_query_extracts_real_symbols_not_stopwords() -> None:
    # Plan 1b: sentence-initial capitals ("How"/"What") must not become anchors
    # (that stranded the graph-local channel). Real code signals must survive.
    u = understand_query("How does retrieve_evidence() build the candidate set?")
    assert "retrieve_evidence" in u.symbols
    assert all(s.lower() not in {"how", "does", "the"} for s in u.symbols)


def test_understand_query_keeps_camel_and_snake_anchors() -> None:
    u = understand_query("Where is QueryPlan defined and how is rerank_mixed used?")
    assert "QueryPlan" in u.symbols
    assert "rerank_mixed" in u.symbols


def test_understand_query_drops_bare_capitalized_words() -> None:
    # A capitalized English word with no internal code signal is not an anchor.
    u = understand_query("What handles errors here?")
    assert "What" not in u.symbols
    assert u.symbols == []


def test_merge_candidates_preserves_exact_and_doc_hits() -> None:
    items = [
        EvidenceCandidate(
            chunk_id=f"h{i}",
            channel="hybrid",
            role="implementation",
            score=100 - i,
            file="anvay/ingest/enricher.py",
            line=i + 1,
            excerpt="enricher",
        )
        for i in range(6)
    ]
    items.extend(
        [
            EvidenceCandidate(
                chunk_id="g1",
                channel="grep",
                role="definition",
                score=3,
                file="anvay/ingest/chunker.py",
                line=29,
                excerpt="MAX_CHUNK_CHARS = 1200",
            ),
            EvidenceCandidate(
                chunk_id="d1",
                channel="grep",
                role="overview",
                score=2,
                file="ENGINEERING.md",
                line=214,
                excerpt="Chunker strategy",
            ),
        ]
    )
    merged = merge_candidates(
        items,
        understanding=understand_query("explain our chunking strategy"),
        top_k=5,
    )
    assert any(c.file == "anvay/ingest/chunker.py" for c in merged)
    assert any(c.file == "ENGINEERING.md" for c in merged)


def test_graph_summary_chunk_is_source_backed_summary() -> None:
    from anvay.graph.extractor import extract_resource_graph

    resource = ResourceRef(source_id="repo", uri="app.py", mime="text/x-python")
    graph = extract_resource_graph(
        product_id="p",
        source_key="src",
        resource=resource,
        content="import os\n\ndef auth():\n    return os.getenv('TOKEN')\n",
    )
    chunk = graph_summary_chunk(product_id="p", resource=resource, extraction=graph)
    assert chunk is not None
    assert is_summary_chunk(chunk)
    assert chunk.resource.uri == "app.py"
    assert "Graph nodes:" in chunk.content
    assert "auth" in chunk.content


def test_graph_community_summary_chunk_captures_relationships() -> None:
    from anvay.graph.extractor import extract_resource_graph

    resource = ResourceRef(source_id="repo", uri="app.py", mime="text/x-python")
    graph = extract_resource_graph(
        product_id="p",
        source_key="src",
        resource=resource,
        content="@router.get('/tokens')\ndef read_token():\n    return {}\n",
    )
    chunk = graph_community_summary_chunk(product_id="p", resource=resource, extraction=graph)

    assert chunk is not None
    assert is_community_summary_chunk(chunk)
    assert "Flows:" in chunk.content


@pytest.mark.asyncio
async def test_summary_candidates_filter_summary_artifacts(monkeypatch) -> None:
    async def fake_retrieve(**_kwargs):
        return RetrievalResult(
            hits=[
                Hit(
                    id="s1",
                    score=0.7,
                    source="rerank",
                    payload={
                        "resource_uri": "app.py",
                        "start_line": 0,
                        "end_line": 0,
                        "content": "Structural summary for app.py.",
                        "artifact_type": "graph_community_summary",
                    },
                ),
                Hit(
                    id="d1",
                    score=0.9,
                    source="rerank",
                    payload={
                        "resource_uri": "app.py",
                        "start_line": 1,
                        "content": "def auth(): pass",
                        "artifact_type": "code",
                    },
                ),
            ],
            reranked=True,
            seed_count=2,
        )

    monkeypatch.setattr(evidence, "retrieve", fake_retrieve)
    out, trace, reranked = await evidence.summary_candidates(
        ctx=object(),
        product_id="p",
        query="explain architecture",
        limit=3,
    )
    assert reranked is True
    assert trace[0].hits == 1
    assert out[0].channel == "summary"
    assert out[0].role == "overview"
    assert out[0].metadata["artifact_type"] == "graph_community_summary"
    assert out[0].line == 0


@pytest.mark.asyncio
async def test_mixed_rerank_reorders_cross_channel_candidates() -> None:
    class FakeReranker:
        async def rerank(self, query, documents, top_k):
            from types import SimpleNamespace

            assert query == "auth"
            assert top_k == 2
            return [
                SimpleNamespace(index=1, score=0.99),
                SimpleNamespace(index=0, score=0.25),
            ]

    candidates = [
        EvidenceCandidate(
            chunk_id="grep-1",
            channel="grep",
            role="definition",
            score=10,
            file="a.py",
            line=1,
            excerpt="literal auth",
        ),
        EvidenceCandidate(
            chunk_id="summary-1",
            channel="summary",
            role="overview",
            score=1,
            file="b.py",
            line=0,
            excerpt="auth architecture",
        ),
    ]
    out, reranked = await evidence.rerank_mixed_candidates(
        ctx=SimpleNamespace(reranker=FakeReranker()),
        query="auth",
        candidates=candidates,
    )
    assert reranked is True
    assert [c.chunk_id for c in out] == ["summary-1", "grep-1"]
    assert out[0].score == 0.99
    assert out[0].metadata["mixed_reranked"] is True


@pytest.mark.asyncio
async def test_retrieve_evidence_drift_lite_adds_query_plan_and_followups(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_retrieve(**kwargs):
        calls.append(kwargs["query"])
        return RetrievalResult(
            hits=[
                Hit(
                    id=f"summary-{len(calls)}",
                    score=0.8,
                    source="rerank",
                    payload={
                        "resource_uri": "ENGINEERING.md",
                        "start_line": 345,
                        "content": "Graph summary for retrieval architecture.",
                        "artifact_type": "summary",
                        "kind": "doc",
                    },
                ),
                Hit(
                    id=f"impl-{len(calls)}",
                    score=0.7,
                    source="rerank",
                    payload={
                        "resource_uri": "anvay/retrieval/evidence.py",
                        "start_line": 101,
                        "content": "async def retrieve_evidence(...):",
                        "kind": "code",
                    },
                ),
            ],
            reranked=True,
            seed_count=2,
        )

    monkeypatch.setattr(evidence, "retrieve", fake_retrieve)
    result = await retrieve_evidence(
        ctx=object(),
        product_id="p",
        query="explain retrieval architecture",
        top_k=6,
        query_mode="drift_lite",
    )

    assert result.query_plan is not None
    assert result.query_plan.mode == "drift_lite"
    assert result.query_plan.shape == "global"
    assert result.query_plan.coverage is not None
    assert result.query_plan.latency_ms >= 0
    assert "drift_lite" in result.query_plan.channels_run
    assert any(t.channel == "drift_lite" for t in result.trace)
    assert len(calls) >= 3


@pytest.mark.asyncio
async def test_retrieve_evidence_latency_budget_skips_drift_lite(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_retrieve(**kwargs):
        calls.append(kwargs["query"])
        return RetrievalResult(
            hits=[
                Hit(
                    id=f"summary-{len(calls)}",
                    score=0.8,
                    source="rerank",
                    payload={
                        "resource_uri": "ENGINEERING.md",
                        "start_line": 345,
                        "content": "Graph summary for retrieval architecture.",
                        "artifact_type": "summary",
                        "kind": "doc",
                    },
                )
            ],
            reranked=True,
            seed_count=1,
        )

    monkeypatch.setattr(evidence, "retrieve", fake_retrieve)
    result = await retrieve_evidence(
        ctx=object(),
        product_id="p",
        query="explain retrieval architecture",
        top_k=6,
        query_mode="drift_lite",
        budget_ms=0.0,  # exhausted immediately -> skip optional stages
    )

    assert result.query_plan is not None
    assert result.query_plan.budget_exceeded is True
    assert "latency_budget_skipped_drift_lite" in result.query_plan.fallbacks
    assert "drift_lite" not in result.query_plan.channels_run
    assert not any(t.channel == "drift_lite" for t in result.trace)


@pytest.mark.asyncio
async def test_retrieve_evidence_latency_budget_skips_coverage_repair(monkeypatch) -> None:
    """Budget exhausted before coverage repair: fallback recorded, repair not run."""

    async def fake_retrieve(**kwargs):
        # Return a single low-role hit so coverage is likely insufficient for a
        # global query (missing 'overview' and 'implementation' facets).
        return RetrievalResult(
            hits=[
                Hit(
                    id="code-1",
                    score=0.6,
                    source="rerank",
                    payload={
                        "resource_uri": "src/main.py",
                        "start_line": 1,
                        "content": "def main(): pass",
                        "kind": "code",
                    },
                )
            ],
            reranked=True,
            seed_count=1,
        )

    monkeypatch.setattr(evidence, "retrieve", fake_retrieve)
    result = await retrieve_evidence(
        ctx=object(),
        product_id="p",
        query="explain our overall architecture and design",
        top_k=6,
        query_mode="global",  # forces global shape; no drift_lite stage
        budget_ms=0.0,  # exhausted immediately -> skip optional stages
    )

    assert result.query_plan is not None
    assert result.query_plan.budget_exceeded is True
    assert "latency_budget_skipped_coverage_repair" in result.query_plan.fallbacks
    assert "coverage_repair" not in result.query_plan.fallbacks


@pytest.mark.asyncio
async def test_retrieve_evidence_combines_hybrid_grep_repomap_graph_and_skills(
    monkeypatch, tmp_path
) -> None:
    cfg = MagicMock()
    cfg.storage.proposal_queue = tmp_path / "proposals.db"
    save_repo_map(
        RepoMap(
            symbols=[
                Symbol(
                    kind="function",
                    name="chunk_resource",
                    file="anvay/ingest/chunker.py",
                    line=249,
                    signature="def chunk_resource(product_id, resource, content)",
                )
            ]
        ),
        repomap_path_for(tmp_path, "p"),
    )

    class FakeIndexer:
        async def search_by_graph_nodes(self, **kwargs):
            assert kwargs["product_id"] == "p"
            return [
                {
                    "id": "graph-1",
                    "score": 1.0,
                    "payload": {
                        "resource_uri": "anvay/ingest/chunker.py",
                        "start_line": 249,
                        "content": "def chunk_resource(product_id, resource, content):",
                        "graph_node_ids": kwargs["graph_node_ids"],
                    },
                }
            ]

    class FakeGraph:
        async def resolve_entity(self, *, product_id, mention, limit):
            return GraphQueryResult(
                nodes=[
                    GraphNode(
                        product_id=product_id,
                        stable_id="file:p:anvay/ingest/chunker.py",
                        labels=["CodeFile"],
                        properties={"resource_uri": "anvay/ingest/chunker.py"},
                        last_seen="2026-01-01T00:00:00+00:00",
                    )
                ]
            )

        async def traverse(self, **kwargs):
            return GraphQueryResult()

    async def fake_retrieve(**kwargs):
        return RetrievalResult(
            hits=[
                Hit(
                    id="hybrid-1",
                    score=0.9,
                    source="rerank",
                    payload={
                        "resource_uri": "ENGINEERING.md",
                        "start_line": 214,
                        "content": "### Chunker\nCode: tree-sitter. Markdown: heading-aware splitter.",
                        "kind": "doc",
                    },
                )
            ],
            reranked=True,
            seed_count=1,
        )

    async def fake_grep_indexed_chunks(**kwargs):
        from anvay.council.state import EvidenceChunk

        return [
            EvidenceChunk(
                chunk_id="grep-1",
                file="anvay/ingest/chunker.py",
                line=29,
                score=30,
                excerpt="MAX_CHUNK_CHARS = 1200",
            )
        ]

    monkeypatch.setattr(evidence, "retrieve", fake_retrieve)
    monkeypatch.setattr(evidence, "grep_indexed_chunks", fake_grep_indexed_chunks)
    ctx = SimpleNamespace(config=cfg, indexer=FakeIndexer())
    skill = Skill(
        name="p-engineering",
        product="p",
        tier="product_master",
        description="Retrieval and chunking guidance.",
        confidence=0.9,
        applies_to=AppliesTo(),
        provenance=Provenance(validated_by="t", validated_at="2026-01-01T00:00:00Z"),
        body="# p-engineering\n\nChunking guidance.",
    )

    result = await retrieve_evidence(
        ctx=ctx,
        graph_store=FakeGraph(),
        product_id="p",
        query="explain anvay/ingest/chunker.py chunking strategy",
        top_k=8,
        skills=[skill],
    )

    assert result.reranked is True
    assert result.coverage.sufficient is True
    assert {c.channel for c in result.candidates} >= {"hybrid", "grep", "repo_map", "graph", "skill"}
    assert any(c.file == "ENGINEERING.md" for c in result.candidates)
    assert any(c.file == "anvay/ingest/chunker.py" for c in result.candidates)
