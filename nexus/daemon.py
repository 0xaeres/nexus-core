"""Continuous index daemon - subscribes to connector update events and
re-indexes affected resources incrementally.

Two phases:

1. Bootstrap: one-shot full ingest across all configured connectors.
2. Watch: loop over `manager.updates()` forever, calling `reindex_resource`
   for each event. Stays up across MCP server crashes (manager reconnects).
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path

from nexus.config import NexusConfig
from nexus.connectors.manager import ConnectorManager
from nexus.graph.store import GraphStore
from nexus.ingest.embedder import EmbedderClient
from nexus.ingest.enricher import ContextualEnricher
from nexus.ingest.incremental import reindex_resource
from nexus.ingest.indexer import Indexer
from nexus.ingest.relation_extractor import RelationExtractor
from nexus.retrieval.cache import SemanticCache

log = logging.getLogger(__name__)


async def run_daemon(
    *,
    config: NexusConfig,
    product_id: str,
    bootstrap: bool = True,
) -> None:
    """Block forever. Caller is responsible for signal handling."""
    async with AsyncExitStack() as stack:
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
        cache = SemanticCache(
            client=indexer.client,
            threshold=config.cache.semantic_threshold,
            ttl_s=config.cache.ttl_hours * 3600,
        )
        graph = GraphStore(
            url=config.graph.url,
            user=config.graph.user,
            password=config.graph.password,
        )
        extractor = RelationExtractor(
            base_url=config.models.light.base_url or "http://localhost:11434",
            model=config.models.light.model,
            extract_docs=config.ingestion.extract_relations.docs,
            extract_code=config.ingestion.extract_relations.code,
        )
        manager = ConnectorManager(config)

        stack.push_async_callback(embedder.aclose)
        stack.push_async_callback(enricher.aclose)
        stack.push_async_callback(extractor.aclose)
        stack.push_async_callback(indexer.aclose)
        stack.push_async_callback(graph.aclose)
        stack.push_async_callback(manager.stop)

        await indexer.ensure_collections()
        await graph.ensure_constraints()
        await manager.start()

        if bootstrap:
            await _bootstrap_sync(
                manager=manager,
                product_id=product_id,
                embedder=embedder,
                enricher=enricher,
                indexer=indexer,
                cache=cache,
                graph=graph,
                extractor=extractor,
                enrich=True,
            )

        log.info("daemon: watching for updates (product=%s)", product_id)
        async for event in manager.updates():
            try:
                content = await _read_with_manager(manager, event)
            except Exception as e:
                log.warning("daemon: read failed for %s: %s", event.resource.uri, e)
                continue
            try:
                await reindex_resource(
                    product_id=product_id,
                    resource=event.resource,
                    content=content,
                    embedder=embedder,
                    enricher=enricher,
                    indexer=indexer,
                    cache=cache,
                    graph=graph,
                    relation_extractor=extractor,
                )
                log.info("daemon: reindexed %s", event.resource.uri)
            except Exception as e:
                log.exception("daemon: reindex failed for %s: %s", event.resource.uri, e)


# ---------------------------------------------------------------- helpers


async def _bootstrap_sync(
    *,
    manager: ConnectorManager,
    product_id: str,
    embedder: EmbedderClient,
    enricher: ContextualEnricher,
    indexer: Indexer,
    cache: SemanticCache,
    graph: GraphStore | None = None,
    extractor: RelationExtractor | None = None,
    enrich: bool,
) -> None:
    log.info("daemon: bootstrap sync (product=%s)", product_id)
    started = datetime.now(UTC)
    count = 0
    async for resource, reader in manager.sync_all(product_id):
        try:
            content = await reader(resource)
        except Exception as e:
            log.debug("bootstrap skip %s: %s", resource.uri, e)
            continue
        try:
            await reindex_resource(
                product_id=product_id,
                resource=resource,
                content=content,
                embedder=embedder,
                enricher=enricher,
                indexer=indexer,
                cache=cache,
                graph=graph,
                relation_extractor=extractor,
                enrich=enrich,
            )
            count += 1
        except Exception as e:
            log.warning("bootstrap reindex failed %s: %s", resource.uri, e)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    log.info("daemon: bootstrap done - %d resources in %.1fs", count, elapsed)


async def _read_with_manager(manager: ConnectorManager, event) -> str:
    """Open a transient client to read the updated resource."""
    from nexus.connectors.local_fs import LocalFsSource
    from nexus.connectors.mcp_client import McpClientHandle

    for state in manager._states.values():
        if event.source_id == f"mcp:{state.cfg.name}":
            async with McpClientHandle(state.cfg) as handle:
                return await handle.read_resource(event.resource.uri)
        if event.source_id == f"local:{state.cfg.name}" and state.cfg.type == "local_fs":
            extras = state.cfg.model_dump(exclude={"name", "type", "watch"})
            src = LocalFsSource(
                __import__("nexus.connectors.local_fs", fromlist=["LocalFsConfig"]).LocalFsConfig(
                    root=Path(extras.get("root", "."))
                )
            )
            return await src.read_resource(event.resource)
    raise RuntimeError(f"no connector for source_id={event.source_id}")
