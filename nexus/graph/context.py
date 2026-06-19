"""Graph-backed internal context pack builders."""

from __future__ import annotations

from collections.abc import Sequence

from nexus.graph.models import (
    CodeContextPack,
    GraphContextEntity,
    GraphEvidenceHit,
    GraphNode,
    GraphStore,
)
from nexus.retrieval.pipeline import RetrievalContext, retrieve

_CODING_EDGE_TYPES = [
    "CONTAINS",
    "DECLARES",
    "IMPORTS",
    "HANDLES",
    "EXPOSES",
    "READS",
    "WRITES",
    "COVERS",
    "DOCUMENTS",
    "CONSTRAINS",
]


async def build_code_context_pack(
    *,
    ctx: RetrievalContext,
    graph_store: GraphStore,
    product_id: str,
    query: str,
    current_file: str | None = None,
    top_k: int = 10,
) -> CodeContextPack:
    """Build internal coding context from graph neighbors plus Qdrant evidence.

    Graph expands likely files/symbols/routes; Qdrant still supplies cited text.
    """
    unknowns: list[str] = []
    resolved_nodes: list[GraphNode] = []
    neighbor_nodes: list[GraphNode] = []
    graph_edges = []

    mentions = _seed_mentions(query=query, current_file=current_file)
    for mention in mentions:
        result = await graph_store.resolve_entity(
            product_id=product_id,
            mention=mention,
            limit=5,
        )
        resolved_nodes.extend(result.nodes)
    resolved_nodes = _dedupe_nodes(resolved_nodes)
    if resolved_nodes:
        traversal = await graph_store.traverse(
            product_id=product_id,
            seed_ids=[node.stable_id for node in resolved_nodes[:8]],
            edge_types=_CODING_EDGE_TYPES,
            max_depth=2,
            limit=40,
        )
        seed_ids = {node.stable_id for node in resolved_nodes}
        neighbor_nodes = [
            node for node in traversal.nodes if node.stable_id not in seed_ids
        ]
        graph_edges = traversal.edges
    else:
        unknowns.append("no graph entity resolved")

    retrieval_query = _retrieval_query(query, current_file, resolved_nodes, neighbor_nodes)
    result = await retrieve(
        ctx=ctx,
        product_id=product_id,
        query=retrieval_query,
        top_k=top_k,
        mode="code",
        graph_node_ids=_graph_node_ids(resolved_nodes, neighbor_nodes),
    )

    return CodeContextPack(
        product_id=product_id,
        query=query,
        current_file=current_file,
        resolved_entities=[_entity(node) for node in resolved_nodes],
        neighbor_entities=[_entity(node) for node in _dedupe_nodes(neighbor_nodes)],
        graph_edges=graph_edges,
        evidence=[_evidence(hit) for hit in result.hits],
        graph_used=bool(resolved_nodes or neighbor_nodes or graph_edges),
        graph_available=True,
        reranked=result.reranked,
        unknowns=unknowns,
    )


def _seed_mentions(*, query: str, current_file: str | None) -> list[str]:
    mentions = []
    if current_file:
        mentions.append(current_file)
    mentions.extend(_tokens(query))
    return _ordered_unique([m for m in mentions if len(m) >= 2])[:8]


def _retrieval_query(
    query: str,
    current_file: str | None,
    resolved_nodes: Sequence[GraphNode],
    neighbor_nodes: Sequence[GraphNode],
) -> str:
    terms = [query]
    if current_file:
        terms.append(current_file)
    for node in [*resolved_nodes[:6], *neighbor_nodes[:10]]:
        props = node.properties
        for key in ("resource_uri", "name", "path", "normalized_path"):
            value = props.get(key)
            if isinstance(value, str) and value:
                terms.append(value)
    return " ".join(_ordered_unique(terms))


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in {"_", "/", ".", "-", ":", "{", "}"}:
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur = []
    if cur:
        out.append("".join(cur))
    return out


def _entity(node: GraphNode) -> GraphContextEntity:
    props = node.properties
    return GraphContextEntity(
        stable_id=node.stable_id,
        labels=node.labels,
        name=str(props.get("name") or props.get("path") or props.get("title") or ""),
        resource_uri=props.get("resource_uri") if isinstance(props.get("resource_uri"), str) else None,
        start_line=props.get("start_line") if isinstance(props.get("start_line"), int) else None,
        end_line=props.get("end_line") if isinstance(props.get("end_line"), int) else None,
        confidence=node.confidence,
    )


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
    resolved_nodes: Sequence[GraphNode],
    neighbor_nodes: Sequence[GraphNode],
) -> list[str]:
    return _ordered_unique(
        [node.stable_id for node in [*resolved_nodes[:8], *neighbor_nodes[:24]]]
    )


def _ordered_unique(values: Sequence[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
