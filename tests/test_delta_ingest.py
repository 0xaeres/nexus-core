from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

import pytest

from nexus.config import EnrichCfg, NexusConfig
from nexus.ingest import pipeline
from nexus.ingest.models import EmbeddedChunk, ResourceRef
from nexus.registry import Registry
from nexus.retrieval.sparse import SparseVector


def _config(tmp_path: Path) -> NexusConfig:
    return NexusConfig(
        models={
            "council": {"provider": "test", "model": "test"},
            "light": {"provider": "test", "model": "test"},
            "embedding": {"provider": "test", "model": "embed-v1", "url": "http://embed"},
            "reranker": {"provider": "test", "model": "test", "url": "http://rerank"},
        },
        storage={
            "proposal_queue": tmp_path / "proposals.db",
            "council_checkpoint": tmp_path / "council.sqlite",
        },
    )


class FakeSource:
    source_id = "local:test"

    def __init__(self, files: dict[str, str]):
        self.files = files

    async def list_resources(self) -> AsyncIterator[ResourceRef]:
        for uri, content in self.files.items():
            yield ResourceRef(
                source_id=self.source_id,
                uri=uri,
                mime="text/plain",
                size_bytes=len(content),
            )

    async def read_resource(self, resource: ResourceRef) -> str:
        return self.files[resource.uri]


class FakeEmbedder:
    calls = 0
    active = 0
    max_active = 0
    delay_s = 0.0

    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def from_cfg(cls, *args, **kwargs):
        return cls()

    async def embed_chunks(self, chunks):
        FakeEmbedder.calls += 1
        FakeEmbedder.active += 1
        FakeEmbedder.max_active = max(FakeEmbedder.max_active, FakeEmbedder.active)
        try:
            if FakeEmbedder.delay_s:
                await asyncio.sleep(FakeEmbedder.delay_s)
            return [
                EmbeddedChunk(chunk=c, vector=[0.1, 0.2], vector_name="dense_text")
                for c in chunks
            ]
        finally:
            FakeEmbedder.active -= 1

    async def aclose(self) -> None:
        pass


class FakeEnricher:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def enrich(self, chunks, *, doc_contents):
        FakeEnricher.calls += 1
        return chunks

    async def aclose(self) -> None:
        pass


class FakeIndexer:
    instances: ClassVar[list[FakeIndexer]] = []

    def __init__(self, *args, **kwargs):
        self.upserted = []
        self.deleted = []
        FakeIndexer.instances.append(self)

    async def ensure_collections(self) -> None:
        pass

    async def upsert(self, embedded, *, sparse_by_id=None, **kwargs):
        self.upserted.append((list(embedded), kwargs))
        return len(embedded)

    async def delete_points_by_ids(self, point_ids):
        self.deleted.extend(point_ids)
        return len(point_ids)

    async def aclose(self) -> None:
        pass


class FakeGraphStore:
    instances: ClassVar[list[FakeGraphStore]] = []

    def __init__(self):
        self.upserted = []
        self.retired = []
        self.closed = False
        FakeGraphStore.instances.append(self)

    async def ensure_schema(self) -> None:
        pass

    async def upsert_resource_graph(self, extraction, *, previous_fact_ids=None):
        self.upserted.append((extraction, list(previous_fact_ids or [])))
        return extraction.fact_ids

    async def retire_resource_graph(self, *, product_id: str, fact_ids: list[str]):
        self.retired.append((product_id, list(fact_ids)))
        return len(fact_ids)

    async def delete_product(self, *, product_id: str):
        return 0

    async def resolve_entity(self, **kwargs):
        raise NotImplementedError

    async def traverse(self, **kwargs):
        raise NotImplementedError

    async def aclose(self) -> None:
        self.closed = True


async def _fake_sparse(texts):
    return [SparseVector(indices=[1], values=[1.0]) for _ in texts]


@pytest.fixture(autouse=True)
def _patch_ingest(monkeypatch):
    FakeEmbedder.calls = 0
    FakeEmbedder.active = 0
    FakeEmbedder.max_active = 0
    FakeEmbedder.delay_s = 0.0
    FakeEnricher.calls = 0
    FakeIndexer.instances = []
    FakeGraphStore.instances = []
    monkeypatch.setattr(pipeline, "EmbedderClient", FakeEmbedder)
    monkeypatch.setattr(pipeline, "ContextualEnricher", FakeEnricher)
    monkeypatch.setattr(pipeline, "create_indexer", lambda config: FakeIndexer())
    monkeypatch.setattr(pipeline, "create_graph_store", lambda config: FakeGraphStore())
    monkeypatch.setattr(pipeline, "aencode_passages", _fake_sparse)


@pytest.mark.asyncio
async def test_delta_sync_skips_unchanged_resources(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    source = FakeSource({"doc.txt": "hello world " * 20})

    first = await pipeline.run_ingest(
        product_id="p",
        source=source,
        config=cfg,
        registry=registry,
        source_key="source",
    )
    second = await pipeline.run_ingest(
        product_id="p",
        source=source,
        config=cfg,
        registry=registry,
        source_key="source",
    )

    assert first.added == 1
    assert first.unchanged == 0
    assert second.added == 0
    assert second.unchanged == 1
    assert FakeEmbedder.calls == 1


@pytest.mark.asyncio
async def test_delta_sync_deletes_stale_ids_after_successful_upsert(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    source = FakeSource({"doc.txt": "new content " * 20})
    version = pipeline.embedding_version(cfg)
    registry.upsert_resource_manifest(
        {
            "product": "p",
            "sourceKey": "source",
            "resourceUri": "doc.txt",
            "contentHash": "old-hash",
            "mime": "text/plain",
            "sizeBytes": 10,
            "lastSeenSync": "old",
            "chunkIds": ["old-chunk-id"],
            "indexedAt": "old",
            "embeddingVersion": version,
        }
    )

    stats = await pipeline.run_ingest(
        product_id="p",
        source=source,
        config=cfg,
        registry=registry,
        source_key="source",
    )

    indexer = FakeIndexer.instances[-1]
    assert stats.updated == 1
    assert indexer.upserted
    assert indexer.deleted == ["old-chunk-id"]
    row = registry.get_resource_manifest("p", "source", "doc.txt")
    assert row is not None
    assert row["contentHash"] != "old-hash"


@pytest.mark.asyncio
async def test_delta_sync_removes_deleted_resources(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    registry.upsert_resource_manifest(
        {
            "product": "p",
            "sourceKey": "source",
            "resourceUri": "gone.txt",
            "contentHash": "old-hash",
            "mime": "text/plain",
            "sizeBytes": 10,
            "lastSeenSync": "old",
            "chunkIds": ["gone-chunk-id"],
            "indexedAt": "old",
            "embeddingVersion": pipeline.embedding_version(cfg),
        }
    )

    stats = await pipeline.run_ingest(
        product_id="p",
        source=FakeSource({}),
        config=cfg,
        registry=registry,
        source_key="source",
    )

    indexer = FakeIndexer.instances[-1]
    assert stats.removed == 1
    assert indexer.deleted == ["gone-chunk-id"]
    assert registry.get_resource_manifest("p", "source", "gone.txt") is None


@pytest.mark.asyncio
async def test_sparse_indexing_uses_enriched_embedding_text(tmp_path: Path, monkeypatch) -> None:
    seen_texts: list[str] = []

    class EnrichingFakeEnricher(FakeEnricher):
        async def enrich(self, chunks, *, doc_contents):
            for chunk in chunks:
                chunk.context_summary = "Q: How is the enriched sparse text indexed?"
            return chunks

    async def record_sparse(texts):
        seen_texts.extend(texts)
        return [SparseVector(indices=[1], values=[1.0]) for _ in texts]

    monkeypatch.setattr(pipeline, "ContextualEnricher", EnrichingFakeEnricher)
    monkeypatch.setattr(pipeline, "aencode_passages", record_sparse)

    cfg = _config(tmp_path)
    await pipeline.run_ingest(
        product_id="p",
        source=FakeSource({"doc.txt": "hello world " * 20}),
        config=cfg,
    )

    assert seen_texts
    assert seen_texts[0].startswith("Q: How is the enriched sparse text indexed?")


@pytest.mark.asyncio
async def test_background_enrichment_queues_without_foreground_llm(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    cfg.ingestion.enrich_chunks = EnrichCfg(docs=True, code=False)

    stats = await pipeline.run_ingest(
        product_id="p",
        source=FakeSource({"doc.txt": "hello world " * 20}),
        config=cfg,
        registry=registry,
        source_key="source",
        enrichment_mode="background",
    )

    indexer = FakeIndexer.instances[-1]
    assert stats.added == 1
    assert indexer.upserted
    assert FakeEnricher.calls == 0
    assert registry.enrichment_job_counts("p")["pending"] == 1


@pytest.mark.asyncio
async def test_background_enrichment_skips_docs_when_doc_enrichment_disabled(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)

    stats = await pipeline.run_ingest(
        product_id="p",
        source=FakeSource({"doc.txt": "hello world " * 20}),
        config=cfg,
        registry=registry,
        source_key="source",
        enrichment_mode="background",
    )

    assert stats.added == 1
    assert FakeEnricher.calls == 0
    assert registry.enrichment_job_counts("p")["pending"] == 0


@pytest.mark.asyncio
async def test_ingest_processes_changed_batches_concurrently(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    cfg.ingestion.file_batch_size = 1
    cfg.ingestion.batch_concurrency = 2
    FakeEmbedder.delay_s = 0.05

    stats = await pipeline.run_ingest(
        product_id="p",
        source=FakeSource(
            {
                "a.txt": "alpha " * 20,
                "b.txt": "bravo " * 20,
                "c.txt": "charlie " * 20,
            }
        ),
        config=cfg,
        registry=registry,
        source_key="source",
        enrichment_mode="disabled",
    )

    assert stats.added == 3
    assert FakeEmbedder.calls == 3
    assert FakeEmbedder.max_active == 2


@pytest.mark.asyncio
async def test_background_enrichment_skips_code_when_hqe_disabled(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)

    stats = await pipeline.run_ingest(
        product_id="p",
        source=FakeSource({"app.py": "def hello():\n    return 'world'\n"}),
        config=cfg,
        registry=registry,
        source_key="source",
        enrichment_mode="background",
    )

    assert stats.added == 1
    assert FakeEnricher.calls == 0
    assert registry.enrichment_job_counts("p")["pending"] == 0


@pytest.mark.asyncio
async def test_delta_sync_writes_graph_manifest_and_payload_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)

    stats = await pipeline.run_ingest(
        product_id="p",
        source=FakeSource(
            {
                "app.py": (
                    "def hello():\n"
                    "    message = 'world ' * 40\n"
                    "    normalized = message.strip().upper()\n"
                    "    return normalized\n"
                )
            }
        ),
        config=cfg,
        registry=registry,
        source_key="source",
        enrichment_mode="disabled",
    )

    row = registry.get_resource_manifest("p", "source", "app.py")
    indexer = FakeIndexer.instances[-1]
    graph = FakeGraphStore.instances[-1]
    _, upsert_kwargs = indexer.upserted[0]
    embedded, _ = indexer.upserted[0]

    assert stats.graph_resources_indexed == 1
    assert row is not None
    assert row["graphStatus"] == "complete"
    assert row["graphExtractionVersion"] == pipeline.graph_extraction_version()
    assert row["graphFactIds"]
    assert graph.upserted
    assert upsert_kwargs["graph_node_ids_by_id"]
    assert upsert_kwargs["source_ref_by_id"]
    artifact_types = upsert_kwargs["artifact_type_by_id"]
    summary_ids = [
        ec.chunk.id
        for ec in embedded
        if ec.chunk.start_line == 0 and ec.chunk.context_path == "Graph summary"
    ]
    assert summary_ids
    assert artifact_types[summary_ids[0]] == "summary"


@pytest.mark.asyncio
async def test_delta_sync_refreshes_stale_graph_without_reembedding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    content = (
        "def hello():\n"
        "    message = 'world ' * 40\n"
        "    normalized = message.strip().upper()\n"
        "    return normalized\n"
    )
    version = pipeline.embedding_version(cfg)
    registry.upsert_resource_manifest(
        {
            "product": "p",
            "sourceKey": "source",
            "resourceUri": "app.py",
            "contentHash": pipeline._content_hash(content),
            "mime": "text/x-python",
            "sizeBytes": len(content),
            "lastSeenSync": "old",
            "chunkIds": ["old-chunk-id"],
            "indexedAt": "old",
            "embeddingVersion": version,
            "graphExtractionVersion": "old",
            "graphStatus": "failed",
            "graphFactIds": ["old-fact-id"],
            "graphIndexedAt": "old",
        }
    )
    stats = await pipeline.run_ingest(
        product_id="p",
        source=FakeSource({"app.py": content}),
        config=cfg,
        registry=registry,
        source_key="source",
        enrichment_mode="disabled",
    )

    row = registry.get_resource_manifest("p", "source", "app.py")
    graph = FakeGraphStore.instances[-1]

    assert stats.unchanged == 1
    assert stats.graph_resources_indexed == 1
    assert FakeEmbedder.calls == 0
    assert not FakeIndexer.instances[-1].upserted
    assert graph.upserted[0][1] == ["old-fact-id"]
    assert row is not None
    assert row["chunkIds"] == ["old-chunk-id"]
    assert row["graphStatus"] == "complete"


@pytest.mark.asyncio
async def test_delta_sync_retires_graph_facts_for_removed_resources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(tmp_path / "registry.db")
    cfg = _config(tmp_path)
    registry.upsert_resource_manifest(
        {
            "product": "p",
            "sourceKey": "source",
            "resourceUri": "gone.py",
            "contentHash": "old-hash",
            "mime": "text/x-python",
            "sizeBytes": 10,
            "lastSeenSync": "old",
            "chunkIds": ["gone-chunk-id"],
            "indexedAt": "old",
            "embeddingVersion": pipeline.embedding_version(cfg),
            "graphExtractionVersion": "old-graph",
            "graphStatus": "complete",
            "graphFactIds": ["fact-1", "fact-2"],
            "graphIndexedAt": "old",
        }
    )
    stats = await pipeline.run_ingest(
        product_id="p",
        source=FakeSource({}),
        config=cfg,
        registry=registry,
        source_key="source",
        enrichment_mode="disabled",
    )

    graph = FakeGraphStore.instances[-1]
    indexer = FakeIndexer.instances[-1]

    assert stats.removed == 1
    assert graph.retired == [("p", ["fact-1", "fact-2"])]
    assert indexer.deleted == ["gone-chunk-id"]
    assert registry.get_resource_manifest("p", "source", "gone.py") is None
