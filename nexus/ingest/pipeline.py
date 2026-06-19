"""End-to-end ingestion pipeline orchestrator.

Pulls resources from a source, chunks them, optionally enriches with contextual
summaries, embeds, and upserts into the configured retrieval index.

Design:
- Files are collected into batches of FILE_BATCH_SIZE before any embedding call.
- Within each batch, reads are concurrent (READ_CONCURRENCY semaphore).
- A bad file is skipped; it does not abort the batch or the run.
- The embedder is called once per batch (not once per file).
- Transient embedder errors are retried with exponential backoff in EmbedderClient.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from nexus.config import NexusConfig
from nexus.graph.extractor import (
    entity_ids_for_chunk,
    extract_resource_graph,
    graph_extraction_version,
    graph_node_ids_for_chunk,
)
from nexus.graph.models import GraphExtraction
from nexus.graph.store import create_graph_store
from nexus.ingest.chunker import chunk_resource
from nexus.ingest.embedder import EmbedderClient, EmbedderError
from nexus.ingest.enricher import ContextualEnricher
from nexus.ingest.indexer_factory import create_indexer
from nexus.ingest.models import Chunk, ChunkKind, ResourceRef
from nexus.ingest.summaries import graph_summary_chunk, is_summary_chunk
from nexus.retrieval.sparse import aencode_passages

log = logging.getLogger(__name__)

IngestEventSink = Callable[[dict], Awaitable[None]]


class _Source(Protocol):
    source_id: str

    async def list_resources(self): ...
    async def read_resource(self, resource: ResourceRef) -> str: ...


class _Registry(Protocol):
    def list_resource_manifests(self, product_id: str, source_key: str) -> list[dict]: ...
    def upsert_resource_manifest(self, row: dict) -> None: ...
    def enqueue_enrichment_job(self, row: dict) -> None: ...
    def update_resource_enrichment(
        self,
        product_id: str,
        source_key: str,
        resource_uri: str,
        *,
        enrichment_version: str,
        enrichment_status: str,
    ) -> bool: ...
    def delete_resource_manifest(
        self, product_id: str, source_key: str, resource_uri: str
    ) -> bool: ...


@dataclass
class IngestStats:
    resources_seen: int = 0
    resources_indexed: int = 0
    resources_skipped: int = 0
    resources_failed: int = 0
    chunks_produced: int = 0
    chunks_indexed: int = 0
    graph_resources_indexed: int = 0
    graph_errors: int = 0
    embed_errors: int = 0  # batches that failed to embed (token limit, server error)
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0


@dataclass
class _ResourcePayload:
    ref: ResourceRef
    content: str
    content_hash: str
    prior: dict | None
    action: str


def embedding_version(config: NexusConfig) -> str:
    """Hash raw index-affecting config. Change => source re-embed."""
    payload = {
        "embedding_provider": config.models.embedding.provider,
        "embedding_model": config.models.embedding.model,
        "embedding_url": config.models.embedding.url,
        "embedding_base_url": config.models.embedding.base_url,
        "embedding_dim": config.models.embedding.dim,
        "embedding_instruction_profile": config.models.embedding.instruction_profile,
        "vector_quantization_enabled": config.vector_store.quantization.enabled,
        "vector_quantization_type": config.vector_store.quantization.type,
        "vector_quantization_bits": config.vector_store.quantization.bits,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def enrichment_version(config: NexusConfig) -> str:
    """Hash enrichment-affecting config. Change => background re-enrich."""
    payload = {
        "light_provider": config.models.light.provider,
        "light_model": config.models.light.model,
        "light_base_url": config.models.light.base_url,
        "enrich_code": config.ingestion.enrich_chunks.code,
        "enrich_docs": config.ingestion.enrich_chunks.docs,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _resource_enrichment_enabled(config: NexusConfig, resource: ResourceRef) -> bool:
    if resource.kind is ChunkKind.CODE:
        return config.ingestion.enrich_chunks.code
    return config.ingestion.enrich_chunks.docs


def _chunks_enrichment_enabled(config: NexusConfig, chunks: list[Chunk]) -> bool:
    return any(
        (chunk.kind is ChunkKind.CODE and config.ingestion.enrich_chunks.code)
        or (chunk.kind is ChunkKind.DOC and config.ingestion.enrich_chunks.docs)
        for chunk in chunks
    )


async def run_ingest(
    *,
    product_id: str,
    source: _Source,
    config: NexusConfig,
    enrich: bool = True,
    enrichment_mode: Literal["foreground", "background", "disabled"] = "foreground",
    event_sink: IngestEventSink | None = None,
    registry: _Registry | None = None,
    source_key: str | None = None,
) -> IngestStats:
    """One pass: discover → batch-read → chunk → enrich → embed → index."""
    stats = IngestStats()

    file_batch_size = config.ingestion.file_batch_size
    read_concurrency = config.ingestion.read_concurrency
    batch_concurrency = max(1, config.ingestion.batch_concurrency)

    embedder = EmbedderClient.from_cfg(
        config.models.embedding,
        batch_size=config.ingestion.embed_batch_size,
    )
    foreground_enrich = enrich and enrichment_mode == "foreground"
    queue_enrichment = enrich and enrichment_mode == "background"
    enricher = (
        ContextualEnricher(
            base_url=config.models.light.base_url or "https://api.deepinfra.com/v1/openai",
            model=config.models.light.model,
            api_key=config.models.light.api_key,
            enrich_code=config.ingestion.enrich_chunks.code,
            enrich_docs=config.ingestion.enrich_chunks.docs,
            concurrency=config.ingestion.enricher_concurrency,
        )
        if foreground_enrich
        else None
    )
    indexer = create_indexer(config)
    graph_store = create_graph_store(config)
    batch_no = 0
    sync_id = _utc_now()
    version = embedding_version(config)
    enrich_version = enrichment_version(config)
    graph_version = graph_extraction_version()
    manifest_by_uri: dict[str, dict] = {}
    current_uris: set[str] = set()
    delta_enabled = registry is not None and source_key is not None
    started_at = time.perf_counter()
    last_emit_at = started_at

    async def emit(level: str, stage: str, msg: str, **extra) -> None:
        nonlocal last_emit_at
        now = time.perf_counter()
        event = {
            "level": level,
            "stage": stage,
            "msg": msg,
            "elapsed_ms": round((now - started_at) * 1000, 1),
            "stage_elapsed_ms": round((now - last_emit_at) * 1000, 1),
            **extra,
        }
        last_emit_at = now
        log.info(
            "ingest.%s product=%s source=%s elapsed_ms=%.1f %s",
            stage,
            product_id,
            source.source_id,
            event["elapsed_ms"],
            msg,
        )
        if event_sink is not None:
            await event_sink(event)

    try:
        await emit("stage", "prepare", "Ensuring vector collections exist")
        await indexer.ensure_collections()
        await graph_store.ensure_schema()
        await emit(
            "stage",
            "graph_prepare",
            "Graph store ready",
            graph_extraction_version=graph_version,
        )
        if delta_enabled:
            manifest_by_uri = {
                row["resourceUri"]: row
                for row in registry.list_resource_manifests(product_id, source_key)
            }
            await emit(
                "stage",
                "manifest",
                f"Loaded {len(manifest_by_uri)} manifest resource(s)",
                source_key=source_key,
                resources=len(manifest_by_uri),
                embedding_version=version,
                graph_extraction_version=graph_version,
            )
        await emit("stage", "discover", "Discovering resources")

        pending: list[_ResourcePayload] = []
        batch_sem = asyncio.Semaphore(batch_concurrency)
        batch_tasks: set[asyncio.Task[None]] = set()
        all_batch_tasks: list[asyncio.Task[None]] = []

        async def flush(items: list[_ResourcePayload]) -> None:
            nonlocal batch_no
            if not items:
                return
            batch_no += 1
            batch_id = batch_no
            task = asyncio.create_task(process_batch(batch_id, list(items)))
            batch_tasks.add(task)
            all_batch_tasks.append(task)
            task.add_done_callback(batch_tasks.discard)
            if len(batch_tasks) >= batch_concurrency * 2:
                done, _pending = await asyncio.wait(
                    batch_tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for completed in done:
                    completed.result()

        async def wait_for_batches() -> None:
            if not all_batch_tasks:
                return
            await asyncio.gather(*all_batch_tasks)

        async def process_batch(batch_id: int, items: list[_ResourcePayload]) -> None:
            async with batch_sem:
                await _process_batch(batch_id, items)

        async def _process_batch(batch_id: int, items: list[_ResourcePayload]) -> None:
            await emit(
                "stage",
                "chunk",
                f"Chunking batch {batch_id}: {len(items)} resource(s)",
                batch=batch_id,
                resources=len(items),
            )

            all_chunks: list[Chunk] = []
            doc_contents: dict[str, str] = {}
            chunks_by_uri: dict[str, list[Chunk]] = {}
            payload_by_uri = {item.ref.uri: item for item in items}
            graph_extractions: dict[str, GraphExtraction] = {}
            graph_failures: dict[str, str] = {}
            for item in items:
                if delta_enabled:
                    try:
                        graph_extractions[item.ref.uri] = extract_resource_graph(
                            product_id=product_id,
                            source_key=source_key,
                            resource=item.ref,
                            content=item.content,
                            indexed_at=sync_id,
                        )
                    except Exception as e:
                        graph_failures[item.ref.uri] = str(e)
                        stats.graph_errors += 1
                        stats.resources_failed += 1
                        log.exception("graph extract failed for %s", item.ref.uri)
                        await emit(
                            "error",
                            "graph_extract",
                            f"Graph extraction failed for {item.ref.uri}: {e}",
                            batch=batch_id,
                            uri=item.ref.uri,
                        )
                chunks = chunk_resource(product_id, item.ref, item.content)
                summary = (
                    graph_summary_chunk(
                        product_id=product_id,
                        resource=item.ref,
                        extraction=graph_extractions[item.ref.uri],
                    )
                    if item.ref.uri in graph_extractions
                    else None
                )
                if summary is not None:
                    chunks.append(summary)
                if not chunks:
                    await emit(
                        "debug",
                        "chunk",
                        f"No chunks produced for {item.ref.uri}",
                        batch=batch_id,
                        uri=item.ref.uri,
                    )
                    stats.resources_skipped += 1
                    continue
                all_chunks.extend(chunks)
                chunks_by_uri[item.ref.uri] = chunks
                doc_contents[item.ref.uri] = item.content

            vector_chunks = [
                c
                for c in all_chunks
                if payload_by_uri[c.resource.uri].action != "graph_refresh"
            ]
            graph_refresh_chunks = [
                c
                for c in all_chunks
                if payload_by_uri[c.resource.uri].action == "graph_refresh"
            ]

            if not all_chunks and not graph_extractions:
                await emit(
                    "stage",
                    "chunk",
                    f"Batch {batch_id} produced no chunks or graph facts",
                    batch=batch_id,
                    chunks=0,
                )
                return

            code_chunks = sum(1 for c in all_chunks if c.kind.value == "code")
            doc_chunks = len(all_chunks) - code_chunks
            await emit(
                "stage",
                "chunk",
                (
                    f"Batch {batch_id} produced {len(all_chunks)} chunk(s) "
                    f"({code_chunks} code, {doc_chunks} docs)"
                ),
                batch=batch_id,
                chunks=len(all_chunks),
                code_chunks=code_chunks,
                doc_chunks=doc_chunks,
            )

            indexed_at = _utc_now()
            n = 0
            embedded = []
            if vector_chunks and foreground_enrich:
                summary_chunks = [chunk for chunk in vector_chunks if is_summary_chunk(chunk)]
                enrichable_chunks = [
                    chunk for chunk in vector_chunks if not is_summary_chunk(chunk)
                ]
                await emit(
                    "stage",
                    "enrich",
                    f"Enriching batch {batch_id}: {len(enrichable_chunks)} chunk(s)",
                    batch=batch_id,
                    chunks=len(enrichable_chunks),
                )
                assert enricher is not None
                enriched_chunks = await enricher.enrich(
                    enrichable_chunks, doc_contents=doc_contents
                )
                vector_chunks = [*enriched_chunks, *summary_chunks]
                enriched = sum(1 for c in vector_chunks if c.context_summary)
                await emit(
                    "stage",
                    "enrich",
                    f"Batch {batch_id} enriched {enriched}/{len(vector_chunks)} chunk(s)",
                    batch=batch_id,
                    chunks=len(vector_chunks),
                    enriched=enriched,
                )
            elif vector_chunks:
                action = "Queueing background enrichment" if queue_enrichment else "Skipping enrichment"
                await emit(
                    "stage",
                    "enrich",
                    f"{action} for batch {batch_id}",
                    batch=batch_id,
                    chunks=len(vector_chunks),
                )

            if vector_chunks:
                try:
                    await emit(
                        "stage",
                        "embed",
                        f"Embedding dense vectors for batch {batch_id}: {len(vector_chunks)} chunk(s)",
                        batch=batch_id,
                        chunks=len(vector_chunks),
                    )
                    embedded = await embedder.embed_chunks(vector_chunks)
                except EmbedderError as e:
                    log.error("embed failed for batch of %d chunks: %s", len(vector_chunks), e)
                    await emit(
                        "error",
                        "embed",
                        f"Embedding failed for batch {batch_id}: {e}",
                        batch=batch_id,
                        chunks=len(vector_chunks),
                    )
                    stats.resources_failed += len(
                        [item for item in items if item.action != "graph_refresh"]
                    )
                    stats.embed_errors += 1
                    return
                await emit(
                    "stage",
                    "embed",
                    f"Batch {batch_id} dense embedding complete",
                    batch=batch_id,
                    chunks=len(embedded),
                )

                sparse_by_id = {}
                if getattr(indexer, "requires_sparse_vectors", True):
                    await emit(
                        "stage",
                        "sparse",
                        f"Encoding BM25 sparse vectors for batch {batch_id}",
                        batch=batch_id,
                        chunks=len(vector_chunks),
                    )
                    sparse_vecs = await aencode_passages(
                        [c.text_for_embedding() for c in vector_chunks]
                    )
                    sparse_by_id = {
                        c.id: sv for c, sv in zip(vector_chunks, sparse_vecs, strict=True)
                    }
                content_hash_by_id = {
                    c.id: payload_by_uri[c.resource.uri].content_hash
                    for c in vector_chunks
                }
                (
                    graph_node_ids_by_id,
                    entity_ids_by_id,
                    source_ref_by_id,
                    citation_anchor_by_id,
                    graph_extraction_version_by_id,
                    artifact_type_by_id,
                ) = _graph_payload_metadata(
                    vector_chunks,
                    graph_extractions=graph_extractions,
                    product_id=product_id,
                    source_key=source_key,
                )
                await emit(
                    "stage",
                    "upsert",
                    f"Upserting batch {batch_id} into vector store",
                    batch=batch_id,
                    chunks=len(embedded),
                )
                n = await indexer.upsert(
                    embedded,
                    sparse_by_id=sparse_by_id,
                    source_key=source_key,
                    content_hash_by_id=content_hash_by_id,
                    graph_node_ids_by_id=graph_node_ids_by_id,
                    entity_ids_by_id=entity_ids_by_id,
                    source_ref_by_id=source_ref_by_id,
                    citation_anchor_by_id=citation_anchor_by_id,
                    graph_extraction_version_by_id=graph_extraction_version_by_id,
                    artifact_type_by_id=artifact_type_by_id,
                    embedding_version=version,
                    indexed_at=indexed_at,
                )
            elif graph_refresh_chunks and hasattr(indexer, "update_payloads"):
                (
                    graph_node_ids_by_id,
                    entity_ids_by_id,
                    source_ref_by_id,
                    citation_anchor_by_id,
                    graph_extraction_version_by_id,
                    artifact_type_by_id,
                ) = _graph_payload_metadata(
                    graph_refresh_chunks,
                    graph_extractions=graph_extractions,
                    product_id=product_id,
                    source_key=source_key,
                )
                await indexer.update_payloads(
                    graph_refresh_chunks,
                    graph_node_ids_by_id=graph_node_ids_by_id,
                    entity_ids_by_id=entity_ids_by_id,
                    source_ref_by_id=source_ref_by_id,
                    citation_anchor_by_id=citation_anchor_by_id,
                    graph_extraction_version_by_id=graph_extraction_version_by_id,
                    artifact_type_by_id=artifact_type_by_id,
                )

            graph_updates: dict[str, tuple[str, list[str], str]] = {}
            if delta_enabled and graph_extractions:
                for uri, extraction in graph_extractions.items():
                    prior = payload_by_uri[uri].prior or {}
                    try:
                        fact_ids = await graph_store.upsert_resource_graph(
                            extraction,
                            previous_fact_ids=prior.get("graphFactIds", []),
                        )
                        graph_updates[uri] = (
                            "complete",
                            fact_ids,
                            indexed_at,
                        )
                        stats.graph_resources_indexed += 1
                    except Exception as e:
                        stats.graph_errors += 1
                        stats.resources_failed += 1
                        log.warning("graph write failed for %s: %s", uri, e)
                        await emit(
                            "error",
                            "graph_upsert",
                            f"Graph write failed for {uri}: {e}",
                            batch=batch_id,
                            uri=uri,
                        )

            indexed_resources = 0
            queued_resources = 0
            manifest_uris = sorted(set(chunks_by_uri) | set(graph_extractions) | set(graph_failures))
            for uri in manifest_uris:
                chunks = chunks_by_uri.get(uri, [])
                item = payload_by_uri[uri]
                vector_changed = item.action != "graph_refresh"
                new_ids = (
                    [c.id for c in chunks]
                    if vector_changed
                    else (item.prior.get("chunkIds", []) if item.prior else [])
                )
                old_ids = item.prior.get("chunkIds", []) if item.prior else []
                stale_ids = sorted(set(old_ids) - set(new_ids)) if vector_changed else []
                should_queue_resource = queue_enrichment and _chunks_enrichment_enabled(
                    config, chunks
                )
                if stale_ids:
                    await emit(
                        "stage",
                        "cleanup_stale",
                        f"Deleting {len(stale_ids)} stale chunk(s) for {uri}",
                        batch=batch_id,
                        uri=uri,
                        chunks=len(stale_ids),
                    )
                    await indexer.delete_points_by_ids(stale_ids)
                if delta_enabled:
                    prior = item.prior or {}
                    graph_status = prior.get("graphStatus", "")
                    graph_fact_ids = prior.get("graphFactIds", [])
                    graph_indexed_at = prior.get("graphIndexedAt", "")
                    graph_manifest_version = prior.get("graphExtractionVersion", "")
                    if uri in graph_updates:
                        graph_status, graph_fact_ids, graph_indexed_at = graph_updates[uri]
                        graph_manifest_version = graph_version
                    elif uri in graph_failures:
                        graph_status = "failed"
                        graph_manifest_version = graph_version
                    registry.upsert_resource_manifest(
                        {
                            "product": product_id,
                            "sourceKey": source_key,
                            "resourceUri": uri,
                            "contentHash": item.content_hash,
                            "mime": item.ref.mime,
                            "sizeBytes": item.ref.size_bytes,
                            "lastSeenSync": sync_id,
                            "chunkIds": new_ids,
                            "indexedAt": indexed_at,
                            "embeddingVersion": version,
                            "enrichmentVersion": (
                                enrich_version
                                if (foreground_enrich or should_queue_resource)
                                else ""
                            ),
                            "enrichmentStatus": (
                                "complete"
                                if foreground_enrich
                                else ("pending" if should_queue_resource else "")
                            ),
                            "graphExtractionVersion": graph_manifest_version,
                            "graphStatus": graph_status,
                            "graphFactIds": graph_fact_ids,
                            "graphIndexedAt": graph_indexed_at,
                        }
                    )
                    await emit(
                        "stage",
                        "manifest_update",
                        f"Manifest updated for {uri}",
                        batch=batch_id,
                        uri=uri,
                        chunks=len(new_ids),
                    )
                    if should_queue_resource:
                        registry.enqueue_enrichment_job(
                            {
                                "product": product_id,
                                "sourceKey": source_key,
                                "resourceUri": uri,
                                "sourceId": item.ref.source_id,
                                "mime": item.ref.mime,
                                "sizeBytes": item.ref.size_bytes,
                                "lastModified": item.ref.last_modified,
                                "contentHash": item.content_hash,
                                "content": item.content,
                            }
                        )
                        queued_resources += 1
                if vector_changed:
                    indexed_resources += 1

            stats.chunks_produced += len(all_chunks)
            stats.chunks_indexed += n
            stats.resources_indexed += indexed_resources
            if queue_enrichment and delta_enabled:
                await emit(
                    "stage",
                    "enrichment_queue",
                    f"Batch {batch_id} queued {queued_resources} resource(s) for enrichment",
                    batch=batch_id,
                    resources=queued_resources,
                )
            await emit(
                "stage",
                "upsert",
                f"Batch {batch_id} indexed {n} chunk(s)",
                batch=batch_id,
                chunks_indexed=n,
                resources_indexed=indexed_resources,
            )

        sem = asyncio.Semaphore(read_concurrency)

        async def read_and_classify(r: ResourceRef) -> _ResourcePayload | None:
            async with sem:
                try:
                    content = await source.read_resource(r)
                except OSError as e:
                    log.debug("skipping %s: %s", r.uri, e)
                    await emit("warn", "read", f"Skipping unreadable resource: {r.uri} ({e})", uri=r.uri)
                    stats.resources_skipped += 1
                    return None

            digest = _content_hash(content)
            prior = manifest_by_uri.get(r.uri) if delta_enabled else None
            if prior and prior["contentHash"] == digest and prior["embeddingVersion"] == version:
                stats.unchanged += 1
                graph_stale = (
                    prior.get("graphExtractionVersion") != graph_version
                    or prior.get("graphStatus") in {"", "pending", "partial", "failed"}
                )
                if graph_stale:
                    await emit(
                        "stage",
                        "graph_diff",
                        f"Graph refresh needed: {r.uri}",
                        uri=r.uri,
                    )
                    return _ResourcePayload(
                        ref=r,
                        content=content,
                        content_hash=digest,
                        prior=prior,
                        action="graph_refresh",
                    )
                await emit("stage", "skip", f"Unchanged: {r.uri}", uri=r.uri)
                if (
                    queue_enrichment
                    and prior.get("enrichmentVersion") != enrich_version
                    and _resource_enrichment_enabled(config, r)
                ):
                    registry.enqueue_enrichment_job(
                        {
                            "product": product_id,
                            "sourceKey": source_key,
                            "resourceUri": r.uri,
                            "sourceId": r.source_id,
                            "mime": r.mime,
                            "sizeBytes": r.size_bytes,
                            "lastModified": r.last_modified,
                            "contentHash": digest,
                            "content": content,
                        }
                    )
                    registry.update_resource_enrichment(
                        product_id,
                        source_key,
                        r.uri,
                        enrichment_version=enrich_version,
                        enrichment_status="pending",
                    )
                    await emit(
                        "stage",
                        "enrichment_queue",
                        f"Queued unchanged resource for enrichment: {r.uri}",
                        uri=r.uri,
                    )
                return None

            action = "added" if prior is None else "updated"
            if action == "added":
                stats.added += 1
            else:
                stats.updated += 1
            await emit("stage", "diff", f"{action.title()}: {r.uri}", action=action, uri=r.uri)
            return _ResourcePayload(
                ref=r,
                content=content,
                content_hash=digest,
                prior=prior,
                action=action,
            )

        resource_batch: list[ResourceRef] = []

        async def flush_reads(resources: list[ResourceRef]) -> None:
            if not resources:
                return
            await emit(
                "stage",
                "read",
                f"Reading {len(resources)} resource(s) for diff",
                resources=len(resources),
            )
            items = await asyncio.gather(*[read_and_classify(r) for r in resources])
            for item in items:
                if item is not None:
                    pending.append(item)
                    if len(pending) >= file_batch_size:
                        await flush(pending)
                        pending.clear()

        async for resource in source.list_resources():
            stats.resources_seen += 1
            current_uris.add(resource.uri)
            resource_batch.append(resource)
            if len(resource_batch) >= file_batch_size:
                await flush_reads(resource_batch)
                resource_batch = []

        await flush_reads(resource_batch)
        await flush(pending)
        await wait_for_batches()

        if delta_enabled:
            removed_rows = [
                row for uri, row in manifest_by_uri.items() if uri not in current_uris
            ]
            for row in removed_rows:
                uri = row["resourceUri"]
                chunk_ids = row.get("chunkIds", [])
                graph_fact_ids = row.get("graphFactIds", [])
                try:
                    await emit(
                        "stage",
                        "delete_removed",
                        f"Deleting removed resource: {uri}",
                        uri=uri,
                        chunks=len(chunk_ids),
                    )
                    if graph_fact_ids:
                        await graph_store.retire_resource_graph(
                            product_id=product_id,
                            fact_ids=graph_fact_ids,
                        )
                    await indexer.delete_points_by_ids(chunk_ids)
                    registry.delete_resource_manifest(product_id, source_key, uri)
                    stats.removed += 1
                except Exception as e:
                    stats.resources_failed += 1
                    log.exception("delete removed resource failed for %s", uri)
                    await emit(
                        "error",
                        "delete_removed",
                        f"Failed deleting removed resource {uri}: {e}",
                        uri=uri,
                    )

            await emit(
                "stage",
                "diff",
                (
                    f"Delta summary: added={stats.added}, updated={stats.updated}, "
                    f"removed={stats.removed}, unchanged={stats.unchanged}"
                ),
                added=stats.added,
                updated=stats.updated,
                removed=stats.removed,
                unchanged=stats.unchanged,
                failed=stats.resources_failed,
            )
        await emit(
            "stage",
            "complete",
            (
                f"Ingest complete: {stats.resources_indexed} resource(s), "
                f"{stats.chunks_indexed} chunk(s)"
            ),
            resources_indexed=stats.resources_indexed,
            resources_skipped=stats.resources_skipped,
            resources_failed=stats.resources_failed,
            chunks_indexed=stats.chunks_indexed,
            graph_resources_indexed=stats.graph_resources_indexed,
            graph_errors=stats.graph_errors,
            embed_errors=stats.embed_errors,
            added=stats.added,
            updated=stats.updated,
            removed=stats.removed,
            unchanged=stats.unchanged,
        )
        return stats
    finally:
        await embedder.aclose()
        if enricher is not None:
            await enricher.aclose()
        await indexer.aclose()
        await graph_store.aclose()


def _graph_payload_metadata(
    chunks: list[Chunk],
    *,
    graph_extractions: dict[str, GraphExtraction],
    product_id: str,
    source_key: str | None,
) -> tuple[
    dict[str, list[str]],
    dict[str, list[str]],
    dict[str, dict],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    graph_node_ids_by_id: dict[str, list[str]] = {}
    entity_ids_by_id: dict[str, list[str]] = {}
    source_ref_by_id: dict[str, dict] = {}
    citation_anchor_by_id: dict[str, str] = {}
    graph_extraction_version_by_id: dict[str, str] = {}
    artifact_type_by_id: dict[str, str] = {}

    for chunk in chunks:
        extraction = graph_extractions.get(chunk.resource.uri)
        if extraction is None:
            continue
        graph_node_ids_by_id[chunk.id] = graph_node_ids_for_chunk(extraction, chunk)
        entity_ids_by_id[chunk.id] = entity_ids_for_chunk(extraction, chunk)
        source_ref_by_id[chunk.id] = {
            "product_id": product_id,
            "source_key": source_key or "",
            "source_id": chunk.resource.source_id,
            "resource_uri": chunk.resource.uri,
            "anchor": chunk.anchor,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
        }
        citation_anchor_by_id[chunk.id] = chunk.anchor
        graph_extraction_version_by_id[chunk.id] = extraction.extraction_version
        artifact_type_by_id[chunk.id] = (
            "summary" if is_summary_chunk(chunk) else chunk.kind.value
        )

    return (
        graph_node_ids_by_id,
        entity_ids_by_id,
        source_ref_by_id,
        citation_anchor_by_id,
        graph_extraction_version_by_id,
        artifact_type_by_id,
    )


async def run_query(
    *,
    product_id: str,
    text: str,
    config: NexusConfig,
    top_k: int = 10,
    mode: str = "auto",
) -> list[dict]:
    """Legacy dense-only query helper; the production path is retrieval.pipeline.retrieve()."""
    embedder = EmbedderClient.from_cfg(config.models.embedding)
    indexer = create_indexer(config)
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
