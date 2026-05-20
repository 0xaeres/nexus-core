"""Stage 4 - GraphRAG expansion (Neo4j 1-2 hop).

For each seed chunk we ask Neo4j for chunks reachable in `hops` REL edges
through shared entities, then merge those neighbours with the seeds via RRF.
The result is a top-N list where graph-reachable evidence wins ties against
pure vector neighbours - exactly the behaviour spec §5 calls for.

If Neo4j is unavailable (graph store passed as None, or `expand_neighbours`
fails), we pass the seeds through unchanged. Slice 5 already wired the
circuit breaker for this path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.retrieval.hybrid import Hit, rrf_merge

if TYPE_CHECKING:
    from nexus.graph.store import GraphStore

log = logging.getLogger(__name__)


async def expand(
    *,
    product_id: str,
    seeds: list[Hit],
    hops: int = 2,
    graph: GraphStore | None = None,
    payload_lookup: dict[str, dict] | None = None,
) -> list[Hit]:
    """Return seeds + graph-reachable neighbour chunks, merged via RRF.

    `payload_lookup` is a {chunk_id: payload} map used to render neighbour
    chunks with the same payload shape as seeds. When None (Slice 5 callers),
    neighbour payloads are populated from Neo4j data only.
    """
    if not seeds or graph is None or hops <= 0:
        return seeds

    seed_ids = [s.id for s in seeds]
    try:
        neighbours = await graph.expand_neighbours(
            product_id=product_id, chunk_ids=seed_ids, hops=hops
        )
    except Exception as e:
        log.warning("graph expand failed: %s", e)
        return seeds

    if not neighbours:
        return seeds

    payload_lookup = payload_lookup or {}
    neighbour_hits: list[Hit] = []
    for n in neighbours:
        existing = payload_lookup.get(n.chunk_id) or {}
        payload = {
            "resource_uri": n.file,
            "start_line": n.line,
            "kind": n.kind,
            "graph_via": n.via_entity,
            "graph_hop": n.hop,
            **existing,
        }
        # Decaying score by hop so closer neighbours win ties.
        neighbour_hits.append(
            Hit(
                id=n.chunk_id,
                score=1.0 / (1.0 + n.hop),
                payload=payload,
                source="graph",
            )
        )

    return rrf_merge([seeds, neighbour_hits], top_k=max(len(seeds), 20))
