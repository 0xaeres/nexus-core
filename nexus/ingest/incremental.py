"""Incremental ingest path used by the continuous index daemon.

Given a (product_id, ResourceRef, content) tuple, this:

1. Asks the indexer for the existing chunk IDs at that resource URI.
2. Deletes those points from Qdrant.
3. Re-chunks the new content, enriches + embeds + sparse-encodes.
4. Upserts the fresh chunks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nexus.ingest.chunker import chunk_resource
from nexus.ingest.embedder import EmbedderClient
from nexus.ingest.enricher import ContextualEnricher
from nexus.ingest.indexer import Indexer
from nexus.ingest.models import ResourceRef
from nexus.retrieval.sparse import aencode_passages

log = logging.getLogger(__name__)


@dataclass
class IncrementalResult:
    chunks_deleted: int
    chunks_indexed: int


async def reindex_resource(
    *,
    product_id: str,
    resource: ResourceRef,
    content: str,
    embedder: EmbedderClient,
    enricher: ContextualEnricher,
    indexer: Indexer,
    enrich: bool = True,
) -> IncrementalResult:
    old_ids = await indexer.delete_by_resource(
        product_id=product_id, resource_uri=resource.uri
    )

    chunks = chunk_resource(product_id, resource, content)
    if enrich and chunks:
        chunks = await enricher.enrich(chunks)

    if not chunks:
        return IncrementalResult(chunks_deleted=len(old_ids), chunks_indexed=0)

    embedded = await embedder.embed_chunks(chunks)
    sparse_vecs = await aencode_passages([c.content for c in chunks])
    sparse_by_id = {c.id: sv for c, sv in zip(chunks, sparse_vecs, strict=True)}
    inserted = await indexer.upsert(embedded, sparse_by_id=sparse_by_id)

    log.info(
        "incremental %s: deleted=%d indexed=%d",
        resource.uri,
        len(old_ids),
        inserted,
    )
    return IncrementalResult(chunks_deleted=len(old_ids), chunks_indexed=inserted)
