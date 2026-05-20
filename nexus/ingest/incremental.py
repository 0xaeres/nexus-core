"""Incremental ingest path used by the continuous index daemon.

Given a (product_id, ResourceRef, content) tuple, this:

1. Asks the indexer for the existing chunk IDs at that resource URI
   (so we can invalidate semantic-cache entries and graph nodes that
   referenced them).
2. Deletes those points from Qdrant + removes their graph nodes.
3. Re-chunks the new content, enriches + embeds + sparse-encodes.
4. Upserts the fresh chunks (vectors + graph nodes + entities + relations).
5. Purges semantic-cache rows whose `chunk_ids` payload intersects step 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nexus.graph.store import GraphStore
from nexus.ingest.chunker import chunk_resource
from nexus.ingest.embedder import EmbedderClient
from nexus.ingest.enricher import ContextualEnricher
from nexus.ingest.indexer import Indexer
from nexus.ingest.models import ResourceRef
from nexus.ingest.relation_extractor import RelationExtractor
from nexus.retrieval.cache import SemanticCache
from nexus.retrieval.sparse import aencode_passages

log = logging.getLogger(__name__)


@dataclass
class IncrementalResult:
    chunks_deleted: int
    chunks_indexed: int
    cache_entries_purged: int
    entities_upserted: int = 0
    relations_upserted: int = 0


async def reindex_resource(
    *,
    product_id: str,
    resource: ResourceRef,
    content: str,
    embedder: EmbedderClient,
    enricher: ContextualEnricher,
    indexer: Indexer,
    cache: SemanticCache,
    relation_extractor: RelationExtractor | None = None,
    graph: GraphStore | None = None,
    enrich: bool = True,
) -> IncrementalResult:
    old_ids = await indexer.delete_by_resource(
        product_id=product_id, resource_uri=resource.uri
    )

    # Detach old graph nodes so dangling MENTIONS don't survive
    if graph is not None:
        for old_id in old_ids:
            try:
                await graph.remove_chunk(product_id=product_id, chunk_id=old_id)
            except Exception as e:
                log.debug("graph remove_chunk failed: %s", e)

    chunks = chunk_resource(product_id, resource, content)
    if enrich and chunks:
        chunks = await enricher.enrich(chunks)

    purged = 0
    if not chunks:
        if old_ids:
            try:
                await cache.purge(product_id=product_id, chunk_ids=old_ids)
                purged = len(old_ids)
            except Exception as e:
                log.debug("cache purge failed: %s", e)
        return IncrementalResult(
            chunks_deleted=len(old_ids),
            chunks_indexed=0,
            cache_entries_purged=purged,
        )

    embedded = await embedder.embed_chunks(chunks)
    sparse_vecs = await aencode_passages([c.content for c in chunks])
    sparse_by_id = {c.id: sv for c, sv in zip(chunks, sparse_vecs, strict=True)}
    inserted = await indexer.upsert(embedded, sparse_by_id=sparse_by_id)

    entities_n = 0
    relations_n = 0
    if graph is not None:
        for chunk in chunks:
            try:
                await graph.upsert_chunk(
                    product_id=product_id,
                    chunk_id=chunk.id,
                    file=chunk.resource.uri,
                    line=chunk.start_line,
                    kind=chunk.kind.value,
                    source_id=chunk.resource.source_id,
                )
            except Exception as e:
                log.debug("graph upsert_chunk failed: %s", e)

        if relation_extractor is not None:
            try:
                extractions = await relation_extractor.extract(chunks)
                for chunk_id, result in extractions.items():
                    await graph.upsert_entities_and_relations(
                        product_id=product_id,
                        chunk_id=chunk_id,
                        entities=result.entities,
                        relations=result.relations,
                    )
                    entities_n += len(result.entities)
                    relations_n += len(result.relations)
            except Exception as e:
                log.warning("relation extraction failed: %s", e)

    if old_ids:
        try:
            await cache.purge(product_id=product_id, chunk_ids=old_ids)
            purged = len(old_ids)
        except Exception as e:
            log.debug("cache purge failed: %s", e)

    log.info(
        "incremental %s: deleted=%d indexed=%d purged=%d entities=%d relations=%d",
        resource.uri,
        len(old_ids),
        inserted,
        purged,
        entities_n,
        relations_n,
    )
    return IncrementalResult(
        chunks_deleted=len(old_ids),
        chunks_indexed=inserted,
        cache_entries_purged=purged,
        entities_upserted=entities_n,
        relations_upserted=relations_n,
    )
