"""Generic product-system GraphRAG engine."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel, Field

from nexus.graph.context import _ordered_unique
from nexus.graph.models import (
    GraphContextEntity,
    GraphEdge,
    GraphNode,
    GraphRAGAnswer,
    GraphRAGCitation,
    GraphRAGPath,
    GraphRAGQuery,
    GraphStore,
)
from nexus.llm.client import ChatClient
from nexus.retrieval.evidence import EvidenceCandidate, retrieve_evidence
from nexus.retrieval.pipeline import RetrievalContext, RetrievalResult, retrieve

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./:{}-]{1,}")
_JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


class _PromptNode(BaseModel):
    stable_id: str
    labels: list[str]
    name: str
    resource_uri: str | None = None
    confidence: float
    source_refs: list[dict] = Field(default_factory=list)


class _PromptEdge(BaseModel):
    stable_id: str
    type: str
    from_id: str
    to_id: str
    confidence: float
    source_refs: list[dict] = Field(default_factory=list)


class _PromptPayload(BaseModel):
    product_id: str
    question: str
    recent_history: list[dict] = Field(default_factory=list)
    resolved_entities: list[_PromptNode] = Field(default_factory=list)
    graph_neighbors: list[_PromptNode] = Field(default_factory=list)
    graph_edges: list[_PromptEdge] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
_ROUTE_RE = re.compile(r"/[A-Za-z0-9_./:{}-]+")
_PATH_RE = re.compile(r"\b[\w./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|md|mdx|sql|yaml|yml|toml)\b")
_STOP_WORDS = {
    "a",
    "about",
    "again",
    "all",
    "an",
    "and",
    "are",
    "can",
    "could",
    "detail",
    "does",
    "explain",
    "flow",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "like",
    "me",
    "of",
    "please",
    "the",
    "this",
    "that",
    "to",
    "what",
    "when",
    "where",
    "why",
    "with",
    "work",
    "works",
}

_GENERIC_EDGE_TYPES = [
    "CONTAINS",
    "DECLARES",
    "IMPORTS",
    "CALLS",
    "DEPENDS_ON",
    "HANDLES",
    "EXPOSES",
    "READS",
    "WRITES",
    "PRODUCES",
    "CONSUMES",
    "COVERS",
    "DOCUMENTS",
    "CONSTRAINS",
    "OWNS",
    "ASSIGNED_TO",
    "IMPLEMENTS",
    "RESOLVES",
    "CHANGED",
    "AFFECTS",
    "MENTIONS",
    "RELATED_TO",
    "PART_OF_FLOW",
]


async def answer_graph_rag(
    *,
    ctx: RetrievalContext,
    graph_store: GraphStore,
    chat: ChatClient | None,
    product_id: str,
    request: GraphRAGQuery,
) -> GraphRAGAnswer:
    """Answer arbitrary product questions via graph expansion + cited retrieval."""
    unknowns: list[str] = []
    resolved_nodes: list[GraphNode] = []
    traversal_nodes: list[GraphNode] = []
    traversal_edges: list[GraphEdge] = []
    graph_paths: list[GraphRAGPath] = []

    resolved_nodes = await _resolve_from_mentions(
        graph_store=graph_store,
        product_id=product_id,
        mentions=_seed_mentions(request),
    )
    if _ambiguous(resolved_nodes) and request.mode not in {"global", "drift_lite"}:
        options = [_entity(node) for node in resolved_nodes[:8]]
        return GraphRAGAnswer(
            product_id=product_id,
            query=request.query,
            answer="Multiple graph entities match this question. Pick a target and ask again.",
            resolved_entities=[],
            graph_available=True,
            graph_used=False,
            needs_clarification=True,
            clarification_options=options,
            confidence=0.0,
            unknowns=["ambiguous entity resolution"],
        )
    if _ambiguous(resolved_nodes):
        resolved_nodes = []
    if not resolved_nodes:
        retrieval_seed = await _retrieve_for_seed(
            ctx=ctx,
            product_id=product_id,
            request=request,
        )
        resolved_nodes = await _resolve_from_retrieval_hits(
            graph_store=graph_store,
            product_id=product_id,
            hits=retrieval_seed.hits,
        )
    if resolved_nodes:
        traversal = await graph_store.traverse(
            product_id=product_id,
            seed_ids=[node.stable_id for node in resolved_nodes[:10]],
            edge_types=_GENERIC_EDGE_TYPES,
            max_depth=max(1, min(request.max_depth, 5)),
            limit=100,
        )
        seed_ids = {node.stable_id for node in resolved_nodes}
        traversal_nodes = [
            node for node in traversal.nodes if node.stable_id not in seed_ids
        ]
        traversal_edges = traversal.edges
        graph_paths = _paths_from_graph(
            seed_nodes=resolved_nodes,
            nodes=traversal.nodes,
            edges=traversal.edges,
        )
    else:
        unknowns.append("no graph entity resolved")

    evidence_result = await _retrieve_answer_evidence(
        ctx=ctx,
        graph_store=graph_store,
        product_id=product_id,
        query=request.query,
        query_mode=request.mode,
        top_k=request.top_k,
        current_file=request.current_file,
        max_depth=request.max_depth,
        resolved_nodes=resolved_nodes,
        traversal_nodes=traversal_nodes,
    )
    citations = _citations_from_result(evidence_result)
    if not citations:
        unknowns.append("no cited evidence found")
    answer = await _synthesize_answer(
        chat=chat,
        product_id=product_id,
        request=request,
        resolved_nodes=resolved_nodes,
        traversal_nodes=traversal_nodes,
        traversal_edges=traversal_edges,
        citations=citations,
        unknowns=unknowns,
    )

    return GraphRAGAnswer(
        product_id=product_id,
        query=request.query,
        answer=answer,
        citations=citations,
        graph_paths=graph_paths,
        resolved_entities=[_entity(node) for node in resolved_nodes],
        confidence=_confidence(resolved_nodes, traversal_nodes, citations, True),
        trace=list(getattr(evidence_result, "trace", []) or []),
        coverage=getattr(evidence_result, "coverage", None),
        query_plan=getattr(evidence_result, "query_plan", None),
        graph_available=True,
        graph_used=bool(resolved_nodes or traversal_nodes or traversal_edges),
        reranked=evidence_result.reranked,
        unknowns=unknowns,
    )


async def _retrieve_answer_evidence(
    *,
    ctx: RetrievalContext,
    graph_store: GraphStore,
    product_id: str,
    query: str,
    query_mode,
    top_k: int,
    current_file: str | None,
    max_depth: int,
    resolved_nodes: Sequence[GraphNode],
    traversal_nodes: Sequence[GraphNode],
):
    if not hasattr(ctx, "indexer"):
        return await retrieve(
            ctx=ctx,
            product_id=product_id,
            query=query,
            top_k=top_k,
            mode="auto",
            graph_node_ids=_evidence_graph_node_ids(resolved_nodes, traversal_nodes),
        )
    return await retrieve_evidence(
        ctx=ctx,
        graph_store=graph_store,
        product_id=product_id,
        query=query,
        top_k=top_k,
        mode="auto",
        current_file=current_file,
        max_depth=max_depth,
        query_mode=query_mode,
    )


def _citations_from_result(result) -> list[GraphRAGCitation]:
    if hasattr(result, "candidates"):
        return [
            _candidate_citation(index, candidate)
            for index, candidate in enumerate(result.candidates, start=1)
        ]
    return [_citation(index, hit) for index, hit in enumerate(result.hits, start=1)]


async def _resolve_from_mentions(
    *,
    graph_store: GraphStore,
    product_id: str,
    mentions: Sequence[str],
) -> list[GraphNode]:
    nodes: list[GraphNode] = []
    for mention in mentions:
        result = await graph_store.resolve_entity(
            product_id=product_id,
            mention=mention,
            limit=6,
        )
        nodes.extend(result.nodes)
    return _dedupe_nodes(nodes)


async def _retrieve_for_seed(
    *,
    ctx: RetrievalContext,
    product_id: str,
    request: GraphRAGQuery,
) -> RetrievalResult:
    return await retrieve(
        ctx=ctx,
        product_id=product_id,
        query=" ".join([request.query, request.current_file or ""]).strip(),
        top_k=min(max(request.top_k, 5), 12),
        mode="auto",
    )


async def _resolve_from_retrieval_hits(
    *,
    graph_store: GraphStore,
    product_id: str,
    hits,
) -> list[GraphNode]:
    graph_ids: list[str] = []
    fallback_mentions: list[str] = []
    for hit in hits:
        payload = hit.payload or {}
        graph_ids.extend(str(v) for v in payload.get("graph_node_ids") or [])
        resource_uri = payload.get("resource_uri")
        if isinstance(resource_uri, str):
            fallback_mentions.append(resource_uri)
    nodes: list[GraphNode] = []
    for mention in _ordered_unique([*graph_ids, *fallback_mentions])[:12]:
        result = await graph_store.resolve_entity(
            product_id=product_id,
            mention=mention,
            limit=3,
        )
        nodes.extend(result.nodes)
    return _dedupe_nodes(nodes)


def _seed_mentions(request: GraphRAGQuery) -> list[str]:
    mentions: list[str] = []
    if request.current_file:
        mentions.append(request.current_file)
    mentions.extend(_PATH_RE.findall(request.query))
    mentions.extend(_JIRA_KEY_RE.findall(request.query))
    mentions.extend(_ROUTE_RE.findall(request.query))
    mentions.extend(
        token for token in _TOKEN_RE.findall(request.query) if _looks_like_explicit_entity(token)
    )
    return [
        token
        for token in _ordered_unique(mentions)
        if len(token) >= 2 and token.lower() not in _STOP_WORDS
    ][:12]


def _looks_like_explicit_entity(token: str) -> bool:
    """Return true for code-ish/user-selected targets, not ordinary prose."""
    if any(marker in token for marker in ("/", ".", ":", "{", "}", "-")):
        return True
    if "_" in token:
        return True
    if re.search(r"[a-z][A-Z]", token):
        return True
    return bool(re.fullmatch(r"[A-Z][A-Za-z0-9]{3,}", token))


def _ambiguous(nodes: Sequence[GraphNode]) -> bool:
    if len(nodes) < 4:
        return False
    labels = {tuple(node.labels) for node in nodes[:6]}
    resources = {node.properties.get("resource_uri") for node in nodes[:6]}
    return len(resources) > 3 or (len(labels) > 1 and len(resources) > 2)


def _expanded_query(
    request: GraphRAGQuery,
    resolved_nodes: Sequence[GraphNode],
    traversal_nodes: Sequence[GraphNode],
) -> str:
    terms = [request.query]
    if request.current_file:
        terms.append(request.current_file)
    for node in [*resolved_nodes[:8], *traversal_nodes[:18]]:
        props = node.properties
        for key in ("resource_uri", "name", "path", "normalized_path", "title", "key", "summary"):
            value = props.get(key)
            if isinstance(value, str) and value:
                terms.append(value)
    return " ".join(_ordered_unique(terms))


def _evidence_graph_node_ids(
    resolved_nodes: Sequence[GraphNode],
    traversal_nodes: Sequence[GraphNode],
) -> list[str]:
    return _ordered_unique(
        [node.stable_id for node in [*resolved_nodes[:10], *traversal_nodes[:40]]]
    )


async def _synthesize_answer(
    *,
    chat: ChatClient | None,
    product_id: str,
    request: GraphRAGQuery,
    resolved_nodes: Sequence[GraphNode],
    traversal_nodes: Sequence[GraphNode],
    traversal_edges: Sequence[GraphEdge],
    citations: Sequence[GraphRAGCitation],
    unknowns: Sequence[str],
) -> str:
    if chat is None:
        return _fallback_answer(
            request=request,
            resolved_nodes=resolved_nodes,
            traversal_nodes=traversal_nodes,
            traversal_edges=traversal_edges,
            citations=citations,
            unknowns=unknowns,
        )
    try:
        resp = await chat.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You answer product-system questions using only supplied graph facts "
                        "and evidence excerpts. Cite material claims with citation ids like [C1]. "
                        "If evidence is missing, say what is unknown. Do not infer causality from "
                        "graph connectivity alone. For broad architecture or flow questions, give a "
                        "complete step-by-step explanation with concrete files/functions when evidence "
                        "supports them; do not answer with a terse symbol summary. If a claim lacks a "
                        "citation, omit it or mark it unknown."
                    ),
                },
                {
                    "role": "user",
                    "content": _prompt_payload(
                        product_id=product_id,
                        request=request,
                        resolved_nodes=resolved_nodes,
                        traversal_nodes=traversal_nodes,
                        traversal_edges=traversal_edges,
                        citations=citations,
                        unknowns=unknowns,
                    ).model_dump_json(indent=2),
                },
            ],
            max_tokens=1600,
            stream=False,
        )
    except Exception as e:
        log.warning("graph rag synthesis failed for product %s: %s", product_id, e)
        return _fallback_answer(
            request=request,
            resolved_nodes=resolved_nodes,
            traversal_nodes=traversal_nodes,
            traversal_edges=traversal_edges,
            citations=citations,
            unknowns=unknowns,
        )
    return resp.content.strip() or _fallback_answer(
        request=request,
        resolved_nodes=resolved_nodes,
        traversal_nodes=traversal_nodes,
        traversal_edges=traversal_edges,
        citations=citations,
        unknowns=unknowns,
    )


def _prompt_payload(
    *,
    product_id: str,
    request: GraphRAGQuery,
    resolved_nodes: Sequence[GraphNode],
    traversal_nodes: Sequence[GraphNode],
    traversal_edges: Sequence[GraphEdge],
    citations: Sequence[GraphRAGCitation],
    unknowns: Sequence[str],
) -> _PromptPayload:
    return _PromptPayload(
        product_id=product_id,
        question=request.query,
        recent_history=[
            message.model_dump(mode="json") for message in request.history[-8:]
        ],
        resolved_entities=[_node_brief(node) for node in resolved_nodes[:12]],
        graph_neighbors=[_node_brief(node) for node in traversal_nodes[:30]],
        graph_edges=[_edge_brief(edge) for edge in traversal_edges[:40]],
        evidence=[citation.model_dump(mode="json") for citation in citations],
        unknowns=list(unknowns),
    )


def _fallback_answer(
    *,
    request: GraphRAGQuery,
    resolved_nodes: Sequence[GraphNode],
    traversal_nodes: Sequence[GraphNode],
    traversal_edges: Sequence[GraphEdge],
    citations: Sequence[GraphRAGCitation],
    unknowns: Sequence[str],
) -> str:
    parts = [f"Question: {request.query}"]
    if resolved_nodes:
        parts.append(
            "Resolved: "
            + ", ".join(_display_name(node) for node in resolved_nodes[:5])
        )
    if traversal_nodes:
        parts.append(
            "Graph context: "
            + ", ".join(_display_name(node) for node in traversal_nodes[:8])
        )
    if traversal_edges:
        parts.append(f"Graph paths include {len(traversal_edges)} edge(s).")
    if citations:
        parts.append(
            "Evidence: "
            + ", ".join(f"[{c.id}] {c.anchor}" for c in citations[:5])
        )
    if unknowns:
        parts.append("Unknowns: " + "; ".join(unknowns))
    if len(parts) == 1:
        parts.append("No product graph or evidence matched this question.")
    return "\n".join(parts)


def _paths_from_graph(
    *,
    seed_nodes: Sequence[GraphNode],
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
) -> list[GraphRAGPath]:
    node_ids = {node.stable_id for node in nodes}
    edge_ids = [edge.stable_id for edge in edges]
    avg_conf = sum(edge.confidence for edge in edges) / max(len(edges), 1)
    return [
        GraphRAGPath(
            seed_id=seed.stable_id,
            node_ids=sorted(node_ids),
            edge_ids=edge_ids,
            summary=f"{_display_name(seed)} expanded to {len(node_ids)} node(s)",
            confidence=round(avg_conf, 3) if edges else seed.confidence,
        )
        for seed in seed_nodes[:5]
    ]


def _citation(index: int, hit) -> GraphRAGCitation:
    payload = hit.payload or {}
    content = str(payload.get("content") or "")
    return GraphRAGCitation(
        id=f"C{index}",
        anchor=f'{payload.get("resource_uri", "?")}:{payload.get("start_line", "?")}',
        source=hit.source,
        context_path=payload.get("context_path"),
        graph_node_ids=list(payload.get("graph_node_ids") or []),
        excerpt=content[:700],
    )


def _candidate_citation(index: int, candidate: EvidenceCandidate) -> GraphRAGCitation:
    return GraphRAGCitation(
        id=f"C{index}",
        anchor=candidate.anchor,
        source=candidate.channel,
        context_path=candidate.context_path,
        graph_node_ids=candidate.graph_node_ids,
        excerpt=candidate.excerpt[:700],
    )


def _entity(node: GraphNode) -> GraphContextEntity:
    props = node.properties
    return GraphContextEntity(
        stable_id=node.stable_id,
        labels=node.labels,
        name=_display_name(node),
        resource_uri=props.get("resource_uri") if isinstance(props.get("resource_uri"), str) else None,
        start_line=props.get("start_line") if isinstance(props.get("start_line"), int) else None,
        end_line=props.get("end_line") if isinstance(props.get("end_line"), int) else None,
        confidence=node.confidence,
    )


def _node_brief(node: GraphNode) -> _PromptNode:
    props = node.properties
    resource_uri = props.get("resource_uri")
    return _PromptNode(
        stable_id=node.stable_id,
        labels=node.labels,
        name=_display_name(node),
        resource_uri=resource_uri if isinstance(resource_uri, str) else None,
        confidence=node.confidence,
        source_refs=[ref.model_dump(mode="json") for ref in node.source_refs[:3]],
    )


def _edge_brief(edge: GraphEdge) -> _PromptEdge:
    return _PromptEdge(
        stable_id=edge.stable_id,
        type=edge.type,
        from_id=edge.from_id,
        to_id=edge.to_id,
        confidence=edge.confidence,
        source_refs=[ref.model_dump(mode="json") for ref in edge.source_refs[:3]],
    )


def _display_name(node: GraphNode) -> str:
    props = node.properties
    return str(
        props.get("name")
        or props.get("path")
        or props.get("title")
        or props.get("key")
        or props.get("resource_uri")
        or node.stable_id
    )


def _confidence(
    resolved_nodes: Sequence[GraphNode],
    traversal_nodes: Sequence[GraphNode],
    citations: Sequence[GraphRAGCitation],
    graph_available: bool,
) -> float:
    if not graph_available and not citations:
        return 0.0
    base = 0.25
    if resolved_nodes:
        base += 0.25
    if traversal_nodes:
        base += 0.2
    if citations:
        base += 0.2
    graph_nodes = [*resolved_nodes, *traversal_nodes]
    avg_graph = (
        sum(node.confidence for node in graph_nodes) / len(graph_nodes)
        if graph_nodes
        else 1.0
    )
    return round(min(base * avg_graph, 0.95), 3)


def _dedupe_nodes(nodes: Sequence[GraphNode]) -> list[GraphNode]:
    by_id = {node.stable_id: node for node in nodes}
    return list(by_id.values())
