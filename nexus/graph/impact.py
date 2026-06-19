"""Graph-backed impact and dependency analysis builders."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from nexus.graph.context import _ordered_unique, _tokens
from nexus.graph.models import (
    AffectedEntity,
    DependencyTrace,
    GraphEvidenceHit,
    GraphNode,
    GraphStore,
    ImpactAnalysis,
)
from nexus.retrieval.pipeline import RetrievalContext, retrieve

_IMPACT_EDGE_TYPES = [
    "IMPORTS",
    "CALLS",
    "DEPENDS_ON",
    "HANDLES",
    "EXPOSES",
    "READS",
    "WRITES",
    "COVERS",
    "DOCUMENTS",
    "CONSTRAINS",
    "OWNS",
    "CHANGED",
    "IMPLEMENTS",
    "RESOLVES",
]

_DEPENDENCY_EDGE_TYPES = [
    "IMPORTS",
    "CALLS",
    "DEPENDS_ON",
    "HANDLES",
    "EXPOSES",
    "READS",
    "WRITES",
    "PRODUCES",
    "CONSUMES",
]


async def build_change_impact(
    *,
    ctx: RetrievalContext,
    graph_store: GraphStore,
    product_id: str,
    query: str,
    changed_files: list[str] | None = None,
    top_k: int = 12,
) -> ImpactAnalysis:
    unknowns: list[str] = []
    seed_nodes: list[GraphNode] = []
    affected_nodes: list[GraphNode] = []
    graph_edges = []

    seed_nodes = await _resolve_seeds(
        graph_store=graph_store,
        product_id=product_id,
        query=query,
        seed_mentions=changed_files or [],
    )
    if seed_nodes:
        traversal = await graph_store.traverse(
            product_id=product_id,
            seed_ids=[node.stable_id for node in seed_nodes[:10]],
            edge_types=_IMPACT_EDGE_TYPES,
            max_depth=3,
            limit=80,
        )
        seed_ids = {node.stable_id for node in seed_nodes}
        affected_nodes = [
            node for node in traversal.nodes if node.stable_id not in seed_ids
        ]
        graph_edges = traversal.edges
    else:
        unknowns.append("no graph seed resolved")

    retrieval_query = _analysis_query(query, changed_files or [], seed_nodes, affected_nodes)
    result = await retrieve(
        ctx=ctx,
        product_id=product_id,
        query=retrieval_query,
        top_k=top_k,
        mode="auto",
        graph_node_ids=_graph_node_ids(seed_nodes, affected_nodes),
    )
    affected = [_affected_entity(node) for node in _dedupe_nodes(affected_nodes)]

    return ImpactAnalysis(
        product_id=product_id,
        query=query,
        changed_files=changed_files or [],
        seed_entities=[_affected_entity(node) for node in seed_nodes],
        affected_entities=affected,
        graph_edges=graph_edges,
        evidence=[_evidence(hit) for hit in result.hits],
        required_checks=_required_checks(affected),
        confidence=_confidence(seed_nodes, affected_nodes, result.hits),
        graph_available=True,
        graph_used=bool(seed_nodes or affected_nodes or graph_edges),
        reranked=result.reranked,
        unknowns=unknowns,
    )


async def build_dependency_trace(
    *,
    ctx: RetrievalContext,
    graph_store: GraphStore,
    product_id: str,
    query: str,
    seeds: list[str] | None = None,
    top_k: int = 10,
) -> DependencyTrace:
    unknowns: list[str] = []
    seed_nodes: list[GraphNode] = []
    neighbor_nodes: list[GraphNode] = []
    graph_edges = []

    seed_nodes = await _resolve_seeds(
        graph_store=graph_store,
        product_id=product_id,
        query=query,
        seed_mentions=seeds or [],
    )
    if seed_nodes:
        traversal = await graph_store.traverse(
            product_id=product_id,
            seed_ids=[node.stable_id for node in seed_nodes[:8]],
            edge_types=_DEPENDENCY_EDGE_TYPES,
            max_depth=3,
            limit=80,
        )
        seed_ids = {node.stable_id for node in seed_nodes}
        neighbor_nodes = [
            node for node in traversal.nodes if node.stable_id not in seed_ids
        ]
        graph_edges = traversal.edges
    else:
        unknowns.append("no graph seed resolved")

    upstream_ids, downstream_ids = _split_dependency_neighbors(
        seed_ids={node.stable_id for node in seed_nodes},
        edges=graph_edges,
    )
    neighbors_by_id = {node.stable_id: node for node in neighbor_nodes}
    retrieval_query = _analysis_query(query, seeds or [], seed_nodes, neighbor_nodes)
    result = await retrieve(
        ctx=ctx,
        product_id=product_id,
        query=retrieval_query,
        top_k=top_k,
        mode="auto",
        graph_node_ids=_graph_node_ids(seed_nodes, neighbor_nodes),
    )

    return DependencyTrace(
        product_id=product_id,
        query=query,
        seed_entities=[_affected_entity(node) for node in seed_nodes],
        upstream=[
            _affected_entity(neighbors_by_id[sid])
            for sid in sorted(upstream_ids)
            if sid in neighbors_by_id
        ],
        downstream=[
            _affected_entity(neighbors_by_id[sid])
            for sid in sorted(downstream_ids)
            if sid in neighbors_by_id
        ],
        graph_edges=graph_edges,
        evidence=[_evidence(hit) for hit in result.hits],
        confidence=_confidence(seed_nodes, neighbor_nodes, result.hits),
        graph_available=True,
        graph_used=bool(seed_nodes or neighbor_nodes or graph_edges),
        reranked=result.reranked,
        unknowns=unknowns,
    )


async def _resolve_seeds(
    *,
    graph_store: GraphStore,
    product_id: str,
    query: str,
    seed_mentions: Sequence[str],
) -> list[GraphNode]:
    nodes: list[GraphNode] = []
    mentions = _ordered_unique([*seed_mentions, *_tokens(query)])[:10]
    results = await asyncio.gather(
        *[
            graph_store.resolve_entity(
                product_id=product_id,
                mention=mention,
                limit=5,
            )
            for mention in mentions
        ]
    )
    for result in results:
        nodes.extend(result.nodes)
    return _dedupe_nodes(nodes)


def _analysis_query(
    query: str,
    seed_mentions: Sequence[str],
    seed_nodes: Sequence[GraphNode],
    neighbor_nodes: Sequence[GraphNode],
) -> str:
    terms = [query, *seed_mentions]
    for node in [*seed_nodes[:8], *neighbor_nodes[:16]]:
        props = node.properties
        for key in ("resource_uri", "name", "path", "normalized_path", "title"):
            value = props.get(key)
            if isinstance(value, str) and value:
                terms.append(value)
    return " ".join(_ordered_unique(terms))


def _split_dependency_neighbors(*, seed_ids: set[str], edges) -> tuple[set[str], set[str]]:
    upstream: set[str] = set()
    downstream: set[str] = set()
    for edge in edges:
        if edge.from_id in seed_ids and edge.to_id not in seed_ids:
            downstream.add(edge.to_id)
        if edge.to_id in seed_ids and edge.from_id not in seed_ids:
            upstream.add(edge.from_id)
    return upstream, downstream


def _affected_entity(node: GraphNode) -> AffectedEntity:
    props = node.properties
    return AffectedEntity(
        stable_id=node.stable_id,
        labels=node.labels,
        name=str(
            props.get("name")
            or props.get("path")
            or props.get("title")
            or props.get("resource_uri")
            or ""
        ),
        resource_uri=props.get("resource_uri") if isinstance(props.get("resource_uri"), str) else None,
        start_line=props.get("start_line") if isinstance(props.get("start_line"), int) else None,
        end_line=props.get("end_line") if isinstance(props.get("end_line"), int) else None,
        confidence=node.confidence,
        category=_category(node),
    )


def _category(node: GraphNode) -> str:
    labels = set(node.labels)
    if labels & {"Service", "UIApp", "UIScreen"}:
        return "service"
    if "APIEndpoint" in labels:
        return "api"
    if labels & {"Test"}:
        return "test"
    if labels & {"Function", "Class", "Module", "CodeFile"}:
        return "code"
    if labels & {"Document", "ADR", "Runbook"}:
        return "doc"
    if labels & {"DBTable", "Migration", "Config", "FeatureFlag", "EventTopic"}:
        return "runtime"
    if labels & {"Owner", "Team"}:
        return "owner"
    if "Actor" in labels:
        return "delivery"
    if labels & {"JiraTicket", "PR", "Commit", "Epic"}:
        return "delivery"
    return "other"


def _required_checks(affected: Sequence[AffectedEntity]) -> list[str]:
    checks = []
    categories = {entity.category for entity in affected}
    if "test" in categories:
        checks.append("Run affected tests.")
    if "api" in categories:
        checks.append("Verify affected API handlers and contracts.")
    if "runtime" in categories:
        checks.append("Review affected config, migrations, or data access.")
    if "doc" in categories:
        checks.append("Review linked docs, ADRs, or runbooks.")
    if not checks:
        checks.append("Validate changed code paths with focused tests.")
    return checks


def _confidence(
    seed_nodes: Sequence[GraphNode],
    affected_nodes: Sequence[GraphNode],
    evidence,
) -> float:
    if not seed_nodes:
        return 0.2 if evidence else 0.0
    base = 0.55
    if affected_nodes:
        base += 0.2
    if evidence:
        base += 0.15
    avg_graph = sum(node.confidence for node in [*seed_nodes, *affected_nodes]) / max(
        len(seed_nodes) + len(affected_nodes), 1
    )
    return round(min(base * avg_graph, 0.95), 3)


def _evidence(hit) -> GraphEvidenceHit:
    payload = hit.payload or {}
    return GraphEvidenceHit(
        score=hit.score,
        source=hit.source,
        anchor=f'{payload.get("resource_uri", "?")}:{payload.get("start_line", "?")}',
        context_path=payload.get("context_path"),
        content=payload.get("content"),
        graph_node_ids=list(payload.get("graph_node_ids") or []),
    )


def _dedupe_nodes(nodes: Sequence[GraphNode]) -> list[GraphNode]:
    by_id = {node.stable_id: node for node in nodes}
    return list(by_id.values())


def _graph_node_ids(
    seed_nodes: Sequence[GraphNode],
    neighbor_nodes: Sequence[GraphNode],
) -> list[str]:
    return _ordered_unique(
        [node.stable_id for node in [*seed_nodes[:10], *neighbor_nodes[:40]]]
    )
