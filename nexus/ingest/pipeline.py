"""End-to-end ingestion pipeline orchestrator.

Pulls resources from a source (currently local-fs only), chunks them, optionally
enriches with contextual summaries, embeds, and upserts into Qdrant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nexus.config import NexusConfig
from nexus.ingest.chunker import chunk_resource
from nexus.ingest.embedder import EmbedderClient
from nexus.ingest.enricher import ContextualEnricher
from nexus.ingest.indexer import Indexer
from nexus.ingest.models import Chunk, ResourceRef
from nexus.retrieval.sparse import aencode_passages


class _Source(Protocol):
    source_id: str

    async def list_resources(self): ...
    async def read_resource(self, resource: ResourceRef) -> str: ...


@dataclass
class IngestStats:
    resources_seen: int = 0
    resources_indexed: int = 0
    resources_skipped: int = 0
    chunks_produced: int = 0
    chunks_indexed: int = 0


async def run_ingest(
    *,
    product_id: str,
    source: _Source,
    config: NexusConfig,
    enrich: bool = True,
) -> IngestStats:
    """One pass over a source: discover → read → chunk → enrich → embed → index."""
    stats = IngestStats()

    embedder = EmbedderClient(
        base_url=config.models.embedding.url or "http://localhost:8080",
        batch_size=config.ingestion.embed_batch_size,
    )
    enricher = ContextualEnricher(
        base_url=config.models.light.base_url or "http://localhost:11434",
        model=config.models.light.model,
        enrich_code=config.ingestion.enrich_chunks.code,
        enrich_docs=config.ingestion.enrich_chunks.docs,
    )
    indexer = Indexer(url=config.vector_store.url)

    try:
        await indexer.ensure_collections()

        async for resource in source.list_resources():
            stats.resources_seen += 1
            try:
                content = await source.read_resource(resource)
            except OSError:
                stats.resources_skipped += 1
                continue

            chunks: list[Chunk] = chunk_resource(product_id, resource, content)
            if not chunks:
                stats.resources_skipped += 1
                continue

            if enrich:
                chunks = await enricher.enrich(chunks)

            embedded = await embedder.embed_chunks(chunks)
            # BM25 sparse vectors: encode chunk content (no enricher prefix)
            sparse_vecs = await aencode_passages([c.content for c in chunks])
            sparse_by_id = {c.id: sv for c, sv in zip(chunks, sparse_vecs, strict=True)}
            n = await indexer.upsert(embedded, sparse_by_id=sparse_by_id)
            stats.chunks_produced += len(chunks)
            stats.chunks_indexed += n
            stats.resources_indexed += 1

        return stats
    finally:
        await embedder.aclose()
        await enricher.aclose()
        await indexer.aclose()


async def run_query(
    *,
    product_id: str,
    text: str,
    config: NexusConfig,
    top_k: int = 10,
    mode: str = "auto",
) -> list[dict]:
    """Dense-only retrieval at this stage. BM25 + GraphRAG + rerank land in Slice 2/6."""
    embedder = EmbedderClient(base_url=config.models.embedding.url or "http://localhost:8080")
    indexer = Indexer(url=config.vector_store.url)
    try:
        vectors_to_search: list[str]
        if mode == "code":
            vectors_to_search = ["dense_code"]
        elif mode == "text":
            vectors_to_search = ["dense_text"]
        else:
            vectors_to_search = ["dense_code", "dense_text"]

        all_hits: list[dict] = []
        for v in vectors_to_search:
            qv = await embedder.embed_query(text, vector=v)  # type: ignore[arg-type]
            hits = await indexer.search_dense(
                product_id=product_id,
                query_vector=qv,
                vector_name=v,
                top_k=top_k,
            )
            for h in hits:
                h["vector_name"] = v
            all_hits.extend(hits)

        all_hits.sort(key=lambda h: h["score"], reverse=True)
        return all_hits[:top_k]
    finally:
        await embedder.aclose()
        await indexer.aclose()
