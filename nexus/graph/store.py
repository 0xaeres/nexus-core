"""Neo4j graph store - product-isolated per §12.

Every node carries:
  - product_id property (filtered in every read query)
  - `Product_<product_id>` label in addition to its type label (defense in depth)

Schema (kept loose; we add constraints rather than a rigid type system):

  (:Chunk    {id, product_id, file, line, kind})
  (:Entity   {id, product_id, type, name})       type in: service | module | symbol |
                                                          ticket | ADR | RFC |
                                                          incident | person
  (:Source   {id, product_id, name, kind})

Edges:
  (:Chunk)-[:MENTIONS]->(:Entity)
  (:Entity)-[:REL {type}]->(:Entity)             type in: implements | references |
                                                          supersedes | owned_by |
                                                          closes | caused_by
  (:Chunk)-[:FROM]->(:Source)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class EntityRef:
    id: str
    type: str
    name: str


@dataclass(frozen=True)
class RelationRef:
    src_id: str
    dst_id: str
    type: str


@dataclass(frozen=True)
class NeighbourChunk:
    chunk_id: str
    file: str
    line: int
    kind: str
    via_entity: str  # which entity bridged from seed to this neighbour
    hop: int


_CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT source_id IF NOT EXISTS FOR (s:Source) REQUIRE s.id IS UNIQUE",
)


class GraphStore:
    """Async Neo4j wrapper. Construct once per process."""

    def __init__(self, url: str, user: str, password: str):
        self._url = url
        self._driver = AsyncGraphDatabase.driver(url, auth=(user, password))
        self._initialised = False

    async def aclose(self) -> None:
        await self._driver.close()

    async def health(self) -> bool:
        try:
            await self._driver.verify_connectivity()
            return True
        except (ServiceUnavailable, Exception):
            return False

    # ------------------------------------------------------------ setup

    async def ensure_constraints(self) -> None:
        if self._initialised:
            return
        try:
            async with self._driver.session() as session:
                for cypher in _CONSTRAINTS:
                    await session.run(cypher)
            self._initialised = True
        except Exception as e:
            log.warning("graph: ensure_constraints failed: %s", e)

    # ------------------------------------------------------------ write

    async def upsert_chunk(
        self,
        *,
        product_id: str,
        chunk_id: str,
        file: str,
        line: int,
        kind: str,
        source_id: str | None = None,
    ) -> None:
        product_label = _product_label(product_id)
        cypher = (
            f"MERGE (c:Chunk:`{product_label}` {{id: $chunk_id}}) "
            "SET c.product_id = $product_id, c.file = $file, "
            "    c.line = $line, c.kind = $kind"
        )
        async with self._driver.session() as session:
            await session.run(
                cypher,
                chunk_id=chunk_id,
                product_id=product_id,
                file=file,
                line=line,
                kind=kind,
            )
            if source_id:
                await session.run(
                    f"MATCH (c:Chunk:`{product_label}` {{id: $chunk_id}}) "
                    f"MERGE (s:Source:`{product_label}` {{id: $source_id}}) "
                    "SET s.product_id = $product_id "
                    "MERGE (c)-[:FROM]->(s)",
                    chunk_id=chunk_id,
                    source_id=source_id,
                    product_id=product_id,
                )

    async def upsert_entities_and_relations(
        self,
        *,
        product_id: str,
        chunk_id: str,
        entities: list[EntityRef],
        relations: list[RelationRef],
    ) -> None:
        if not entities and not relations:
            return
        product_label = _product_label(product_id)

        async with self._driver.session() as session:
            for ent in entities:
                await session.run(
                    f"MERGE (e:Entity:`{product_label}` {{id: $id}}) "
                    "SET e.product_id = $product_id, e.type = $type, e.name = $name "
                    "WITH e "
                    f"MATCH (c:Chunk:`{product_label}` {{id: $chunk_id}}) "
                    "MERGE (c)-[:MENTIONS]->(e)",
                    id=ent.id,
                    type=ent.type,
                    name=ent.name,
                    product_id=product_id,
                    chunk_id=chunk_id,
                )
            for rel in relations:
                await session.run(
                    f"MATCH (a:Entity:`{product_label}` {{id: $src}}), "
                    f"      (b:Entity:`{product_label}` {{id: $dst}}) "
                    "MERGE (a)-[r:REL {type: $type}]->(b) "
                    "SET r.product_id = $product_id",
                    src=rel.src_id,
                    dst=rel.dst_id,
                    type=rel.type,
                    product_id=product_id,
                )

    async def remove_chunk(self, *, product_id: str, chunk_id: str) -> None:
        product_label = _product_label(product_id)
        async with self._driver.session() as session:
            await session.run(
                f"MATCH (c:Chunk:`{product_label}` {{id: $id, product_id: $product_id}}) "
                "DETACH DELETE c",
                id=chunk_id,
                product_id=product_id,
            )

    # ------------------------------------------------------------ read

    async def expand_neighbours(
        self,
        *,
        product_id: str,
        chunk_ids: list[str],
        hops: int = 2,
        limit_per_seed: int = 5,
    ) -> list[NeighbourChunk]:
        """For each seed chunk, follow MENTIONS -> REL* -> MENTIONS back to
        chunks, return distinct neighbours (excluding the seeds themselves)."""
        if not chunk_ids or hops <= 0:
            return []
        product_label = _product_label(product_id)
        hops_clause = f"*1..{int(hops)}"

        cypher = (
            f"MATCH (seed:Chunk:`{product_label}` {{product_id: $product_id}}) "
            "WHERE seed.id IN $seed_ids "
            "MATCH (seed)-[:MENTIONS]->(e:Entity {product_id: $product_id}) "
            f"MATCH (e)-[:REL{hops_clause}]-(e2:Entity {{product_id: $product_id}}) "
            "MATCH (neighbour:Chunk {product_id: $product_id})-[:MENTIONS]->(e2) "
            "WHERE NOT neighbour.id IN $seed_ids "
            "WITH neighbour, e.name AS via, "
            "     min(length(shortestPath((e)-[:REL*]-(e2)))) AS hop "
            "RETURN neighbour.id AS chunk_id, neighbour.file AS file, "
            "       neighbour.line AS line, neighbour.kind AS kind, "
            "       via, hop "
            "ORDER BY hop ASC "
            "LIMIT $limit"
        )
        limit = max(1, limit_per_seed * len(chunk_ids))
        try:
            async with self._driver.session() as session:
                result = await session.run(
                    cypher,
                    seed_ids=chunk_ids,
                    product_id=product_id,
                    limit=limit,
                )
                records = [r async for r in result]
        except Exception as e:
            log.debug("graph expand failed: %s", e)
            return []

        return [
            NeighbourChunk(
                chunk_id=r["chunk_id"],
                file=r["file"] or "?",
                line=int(r["line"] or 0),
                kind=r["kind"] or "code",
                via_entity=r["via"] or "",
                hop=int(r["hop"] or 1),
            )
            for r in records
        ]

    async def count_nodes(self, *, product_id: str) -> dict[str, int]:
        product_label = _product_label(product_id)
        cypher = (
            f"MATCH (n:`{product_label}`) "
            "RETURN labels(n) AS labels, count(*) AS n"
        )
        out = {"chunks": 0, "entities": 0, "sources": 0}
        try:
            async with self._driver.session() as session:
                result = await session.run(cypher)
                async for r in result:
                    labels: list[str] = list(r["labels"])
                    if "Chunk" in labels:
                        out["chunks"] += int(r["n"])
                    elif "Entity" in labels:
                        out["entities"] += int(r["n"])
                    elif "Source" in labels:
                        out["sources"] += int(r["n"])
        except Exception as e:
            log.debug("graph count failed: %s", e)
        return out


# ---------------------------------------------------------------- helpers


def _product_label(product_id: str) -> str:
    """Backtick-safe label name: only [A-Za-z0-9_]."""
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in product_id)
    return f"Product_{safe}"


def driver_health(url: str) -> dict[str, Any]:  # pragma: no cover - utility
    return {"url": url}
