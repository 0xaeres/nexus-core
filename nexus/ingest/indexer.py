"""Qdrant indexer — named vectors, per-product payload isolation.

Two collections (per nexus.yaml `vector_store.collections`):

  nexus_code   stores code chunks; named vectors: {dense, bm25}
  nexus_text   stores doc chunks;  named vectors: {dense, bm25}

`dense`  — configured embedding model dimensionality, cosine
`bm25`   — fastembed Qdrant/bm25 sparse encoder; Qdrant applies IDF server-side

Tenant isolation via `product_id` payload filter on every query/scroll/delete.
A keyword index on `product_id` makes these filters fast.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import TypeVar

import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import exceptions as qex
from qdrant_client.http import models as qm

from nexus.ingest.models import Chunk, EmbeddedChunk
from nexus.retrieval.sparse import SparseVector, aencode_query

# Default dense embedding dimensionality. Configurable per-collection on create.
DEFAULT_VECTOR_DIM = 2048

_CODE_COLLECTION = "nexus_code"
_TEXT_COLLECTION = "nexus_text"
_QDRANT_RETRY_DELAYS = (1.0, 3.0, 8.0)

log = logging.getLogger(__name__)
T = TypeVar("T")


class IndexerError(RuntimeError):
    pass


def _point_batches(
    points: Sequence[qm.PointStruct], batch_size: int
) -> list[Sequence[qm.PointStruct]]:
    return [points[i : i + batch_size] for i in range(0, len(points), batch_size)]


def _exception_detail(exc: Exception | None) -> str:
    if exc is None:
        return "unknown error"
    return str(exc) or repr(exc)


class Indexer:
    """Async Qdrant wrapper. Construct once per process."""

    requires_sparse_vectors = True

    def __init__(
        self,
        url: str = "http://localhost:6333",
        *,
        code_collection: str = _CODE_COLLECTION,
        text_collection: str = _TEXT_COLLECTION,
        vector_dim: int = DEFAULT_VECTOR_DIM,
        timeout_s: int = 120,
        upsert_batch_size: int = 16,
        quantization_enabled: bool = True,
        quantization_type: str = "turboquant",
        quantization_bits: str = "bits4",
        quantization_always_ram: bool = True,
    ):
        self.client = AsyncQdrantClient(
            url=url, check_compatibility=False, timeout=timeout_s
        )
        self._code = code_collection
        self._text = text_collection
        self._dim = vector_dim
        self._upsert_batch_size = max(1, upsert_batch_size)
        self._quantization_enabled = quantization_enabled
        self._quantization_type = quantization_type
        self._quantization_bits = quantization_bits
        self._quantization_always_ram = quantization_always_ram

    async def aclose(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------ setup

    async def ensure_collections(self) -> None:
        """Idempotent: create collections with named vectors + custom shard key."""
        collections = await self._retry_qdrant(
            "get_collections", self.client.get_collections
        )
        existing = {c.name for c in collections.collections}
        for name in (self._code, self._text):
            if name not in existing:
                await self._retry_qdrant(
                    f"create_collection:{name}",
                    lambda name=name: self.client.create_collection(
                        collection_name=name,
                        vectors_config={
                            "dense": qm.VectorParams(
                                size=self._dim,
                                distance=qm.Distance.COSINE,
                                hnsw_config=qm.HnswConfigDiff(m=16, ef_construct=128),
                                quantization_config=self._dense_quantization_config(),
                            ),
                        },
                        sparse_vectors_config={
                            "bm25": qm.SparseVectorParams(
                                modifier=qm.Modifier.IDF,
                            ),
                        },
                    ),
                )
            await self._ensure_payload_indexes(name)

    # ------------------------------------------------------------ write

    async def upsert(
        self,
        embedded: Sequence[EmbeddedChunk],
        *,
        sparse_by_id: dict[str, SparseVector] | None = None,
        source_key: str | None = None,
        content_hash_by_id: dict[str, str] | None = None,
        graph_node_ids_by_id: dict[str, list[str]] | None = None,
        entity_ids_by_id: dict[str, list[str]] | None = None,
        source_ref_by_id: dict[str, dict] | None = None,
        citation_anchor_by_id: dict[str, str] | None = None,
        graph_extraction_version_by_id: dict[str, str] | None = None,
        artifact_type_by_id: dict[str, str] | None = None,
        embedding_version: str | None = None,
        indexed_at: str | None = None,
    ) -> int:
        """Upsert dense + (optional) sparse vectors per chunk."""
        if not embedded:
            return 0
        sparse_by_id = sparse_by_id or {}
        content_hash_by_id = content_hash_by_id or {}
        graph_node_ids_by_id = graph_node_ids_by_id or {}
        entity_ids_by_id = entity_ids_by_id or {}
        source_ref_by_id = source_ref_by_id or {}
        citation_anchor_by_id = citation_anchor_by_id or {}
        graph_extraction_version_by_id = graph_extraction_version_by_id or {}
        artifact_type_by_id = artifact_type_by_id or {}
        buckets: dict[tuple[str, str], list[qm.PointStruct]] = {}
        for ec in embedded:
            coll = self._code if ec.vector_name == "dense_code" else self._text
            point = self._to_point(
                ec,
                sparse_by_id.get(ec.chunk.id),
                source_key=source_key,
                content_hash=content_hash_by_id.get(ec.chunk.id),
                graph_node_ids=graph_node_ids_by_id.get(ec.chunk.id),
                entity_ids=entity_ids_by_id.get(ec.chunk.id),
                source_ref=source_ref_by_id.get(ec.chunk.id),
                citation_anchor=citation_anchor_by_id.get(ec.chunk.id),
                graph_extraction_version=graph_extraction_version_by_id.get(ec.chunk.id),
                artifact_type=artifact_type_by_id.get(ec.chunk.id),
                embedding_version=embedding_version,
                indexed_at=indexed_at,
            )
            buckets.setdefault((coll, ec.chunk.product_id), []).append(point)

        n = 0
        for (coll, _product_id), points in buckets.items():
            for batch in _point_batches(points, self._upsert_batch_size):
                await self._retry_qdrant(
                    f"upsert:{coll}",
                    lambda coll=coll, batch=batch: self.client.upsert(
                        collection_name=coll,
                        points=batch,
                    ),
                )
                n += len(batch)
        return n

    async def delete_points_by_ids(
        self, point_ids: Sequence[str] | dict[str, Sequence[str]]
    ) -> int:
        """Delete points by ID from one or both collections."""
        if isinstance(point_ids, dict):
            buckets = point_ids
        else:
            buckets = {self._code: point_ids, self._text: point_ids}

        deleted = 0
        for coll, ids in buckets.items():
            unique_ids = sorted(set(ids))
            if not unique_ids:
                continue
            await self._retry_qdrant(
                f"delete_points:{coll}",
                lambda coll=coll, unique_ids=unique_ids: self.client.delete(
                    collection_name=coll,
                    points_selector=qm.PointIdsList(points=list(unique_ids)),
                ),
            )
            deleted += len(unique_ids)
        return deleted

    async def update_payloads(
        self,
        chunks: Sequence[Chunk],
        *,
        graph_node_ids_by_id: dict[str, list[str]] | None = None,
        entity_ids_by_id: dict[str, list[str]] | None = None,
        source_ref_by_id: dict[str, dict] | None = None,
        citation_anchor_by_id: dict[str, str] | None = None,
        graph_extraction_version_by_id: dict[str, str] | None = None,
        artifact_type_by_id: dict[str, str] | None = None,
    ) -> int:
        """Patch graph-derived payload metadata without rewriting vectors."""
        graph_node_ids_by_id = graph_node_ids_by_id or {}
        entity_ids_by_id = entity_ids_by_id or {}
        source_ref_by_id = source_ref_by_id or {}
        citation_anchor_by_id = citation_anchor_by_id or {}
        graph_extraction_version_by_id = graph_extraction_version_by_id or {}
        artifact_type_by_id = artifact_type_by_id or {}

        updated = 0
        for chunk in chunks:
            payload = {
                "graph_node_ids": graph_node_ids_by_id.get(chunk.id, []),
                "entity_ids": entity_ids_by_id.get(chunk.id, []),
                "source_ref": source_ref_by_id.get(chunk.id, {}),
                "citation_anchor": citation_anchor_by_id.get(chunk.id, chunk.anchor),
                "graph_extraction_version": graph_extraction_version_by_id.get(
                    chunk.id
                ),
                "artifact_type": artifact_type_by_id.get(chunk.id, chunk.kind.value),
            }
            collection = self._code if chunk.kind.value == "code" else self._text
            await self._retry_qdrant(
                f"set_payload:{collection}",
                lambda collection=collection, payload=payload, chunk_id=chunk.id: self.client.set_payload(
                    collection_name=collection,
                    payload=payload,
                    points=[chunk_id],
                ),
            )
            updated += 1
        return updated

    # ------------------------------------------------------------ read

    async def search_dense(
        self,
        *,
        product_id: str,
        query_vector: list[float],
        vector_name: str,
        top_k: int = 50,
    ) -> list[dict]:
        coll = self._code if vector_name == "dense_code" else self._text
        return await self._search(
            collection=coll,
            product_id=product_id,
            query=query_vector,
            using="dense",
            top_k=top_k,
        )

    async def search_sparse(
        self,
        *,
        product_id: str,
        sparse: SparseVector | None = None,
        query: str | None = None,
        vector_kind: str,
        top_k: int = 50,
    ) -> list[dict]:
        """Sparse BM25 search. `vector_kind` is 'code' or 'text'."""
        if sparse is None:
            if query is None:
                raise ValueError("search_sparse requires sparse or query")
            sparse = await aencode_query(query)
        coll = self._code if vector_kind == "code" else self._text
        return await self._search(
            collection=coll,
            product_id=product_id,
            query=qm.SparseVector(indices=sparse.indices, values=sparse.values),
            using="bm25",
            top_k=top_k,
        )

    async def search_by_graph_nodes(
        self,
        *,
        product_id: str,
        graph_node_ids: Sequence[str],
        vector_kind: str | None = None,
        top_k: int = 50,
    ) -> list[dict]:
        """Return chunks directly attached to graph nodes, product-scoped."""
        ids = sorted({gid for gid in graph_node_ids if gid})
        if not ids:
            return []
        collections = []
        if vector_kind in (None, "code"):
            collections.append(self._code)
        if vector_kind in (None, "text"):
            collections.append(self._text)
        hits: list[dict] = []
        for coll in collections:
            points, _ = await self._retry_qdrant(
                f"scroll_graph_nodes:{coll}",
                lambda coll=coll: self.client.scroll(
                    collection_name=coll,
                    scroll_filter=qm.Filter(
                        must=[
                            qm.FieldCondition(
                                key="product_id",
                                match=qm.MatchValue(value=product_id),
                            ),
                            qm.FieldCondition(
                                key="graph_node_ids",
                                match=qm.MatchAny(any=ids),
                            ),
                        ]
                    ),
                    limit=top_k,
                    with_payload=True,
                    with_vectors=False,
                ),
            )
            hits.extend(
                {
                    "id": pt.id,
                    "score": 1.0,
                    "payload": pt.payload,
                    "collection": coll,
                }
                for pt in points
            )
        return hits[:top_k]

    async def _search(
        self,
        *,
        collection: str,
        product_id: str,
        query,
        using: str,
        top_k: int,
    ) -> list[dict]:
        result = await self._retry_qdrant(
            f"query_points:{collection}",
            lambda: self.client.query_points(
                collection_name=collection,
                query=query,
                using=using,
                limit=top_k,
                query_filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="product_id", match=qm.MatchValue(value=product_id)
                        )
                    ]
                ),
                with_payload=True,
            ),
        )
        return [
            {"id": pt.id, "score": pt.score, "payload": pt.payload}
            for pt in result.points
        ]

    async def count(self, *, product_id: str, vector_kind: str) -> int:
        coll = self._code if vector_kind == "code" else self._text
        res = await self._retry_qdrant(
            f"count:{coll}",
            lambda: self.client.count(
                collection_name=coll,
                count_filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="product_id", match=qm.MatchValue(value=product_id)
                        )
                    ]
                ),
                exact=True,
            ),
        )
        return res.count

    async def iter_chunk_payloads(
        self,
        *,
        product_id: str,
        vector_kind: str,
        batch_size: int = 256,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Yield indexed chunk payloads for one product/kind without vectors."""
        coll = self._code if vector_kind == "code" else self._text
        product_filter = qm.Filter(
            must=[
                qm.FieldCondition(
                    key="product_id", match=qm.MatchValue(value=product_id)
                )
            ]
        )
        offset = None
        while True:
            points, offset = await self._retry_qdrant(
                f"scroll:{coll}",
                lambda offset=offset: self.client.scroll(
                    collection_name=coll,
                    scroll_filter=product_filter,
                    limit=batch_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                ),
            )
            for point in points:
                yield str(point.id), point.payload or {}
            if offset is None:
                break

    async def delete_by_resource(
        self, *, product_id: str, resource_uri: str
    ) -> list[str]:
        """Delete all points for a resource. Returns the deleted IDs for cache
        invalidation hooks."""
        deleted: list[str] = []
        for coll in (self._code, self._text):
            # Scroll for the IDs before we delete, since `delete` doesn't echo them.
            scrolled, _ = await self._retry_qdrant(
                f"scroll_delete_resource:{coll}",
                lambda coll=coll: self.client.scroll(
                    collection_name=coll,
                    scroll_filter=qm.Filter(
                        must=[
                            qm.FieldCondition(
                                key="product_id", match=qm.MatchValue(value=product_id)
                            ),
                            qm.FieldCondition(
                                key="resource_uri",
                                match=qm.MatchValue(value=resource_uri),
                            ),
                        ]
                    ),
                    limit=1024,
                    with_payload=False,
                    with_vectors=False,
                ),
            )
            if not scrolled:
                continue
            ids = [str(pt.id) for pt in scrolled]
            deleted.extend(ids)
            await self._retry_qdrant(
                f"delete_resource:{coll}",
                lambda coll=coll, scrolled=scrolled: self.client.delete(
                    collection_name=coll,
                    points_selector=qm.PointIdsList(points=[pt.id for pt in scrolled]),
                ),
            )
        return deleted

    async def delete_by_product(self, *, product_id: str) -> dict[str, int]:
        """Delete all points for a product from code/text collections."""
        counts: dict[str, int] = {}
        product_filter = qm.Filter(
            must=[
                qm.FieldCondition(
                    key="product_id", match=qm.MatchValue(value=product_id)
                )
            ]
        )
        for coll in (self._code, self._text):
            before = await self._retry_qdrant(
                f"count_delete_product:{coll}",
                lambda coll=coll: self.client.count(
                    collection_name=coll,
                    count_filter=product_filter,
                    exact=True,
                ),
            )
            if before.count:
                await self._retry_qdrant(
                    f"delete_product:{coll}",
                    lambda coll=coll: self.client.delete(
                        collection_name=coll,
                        points_selector=qm.FilterSelector(filter=product_filter),
                    ),
                )
            counts[coll] = before.count
        return counts

    async def _retry_qdrant(
        self, operation: str, call: Callable[[], Awaitable[T]]
    ) -> T:
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_QDRANT_RETRY_DELAYS, None), start=1):
            try:
                return await call()
            except (httpx.HTTPError, qex.ResponseHandlingException) as e:
                last_exc = e
                if delay is None:
                    break
                detail = _exception_detail(e)
                log.warning(
                    "qdrant %s attempt %d failed; retrying in %.0fs: %s",
                    operation,
                    attempt,
                    delay,
                    detail,
                )
                await asyncio.sleep(delay)
        raise IndexerError(
            f"qdrant {operation} failed after retries: {_exception_detail(last_exc)}"
        ) from last_exc

    async def _ensure_payload_indexes(self, collection: str) -> None:
        indexes = {
            "product_id": qm.PayloadSchemaType.KEYWORD,
            "resource_uri": qm.PayloadSchemaType.KEYWORD,
            "graph_node_ids": qm.PayloadSchemaType.KEYWORD,
            "entity_ids": qm.PayloadSchemaType.KEYWORD,
            "artifact_type": qm.PayloadSchemaType.KEYWORD,
            "source_ref.resource_uri": qm.PayloadSchemaType.KEYWORD,
        }
        for field_name, field_schema in indexes.items():
            try:
                await self._retry_qdrant(
                    "create_payload_index",
                    lambda collection=collection, field_name=field_name, field_schema=field_schema: self.client.create_payload_index(
                        collection_name=collection,
                        field_name=field_name,
                        field_schema=field_schema,
                    ),
                )
            except Exception as e:
                if "already" not in str(e).lower():
                    raise

    # ------------------------------------------------------------ helpers

    def _dense_quantization_config(self) -> qm.QuantizationConfig | None:
        if not self._quantization_enabled:
            return None
        if self._quantization_type.lower() not in {"turboquant", "turbo"}:
            raise IndexerError(
                f"unsupported vector_store.quantization.type: {self._quantization_type}"
            )
        bits_by_name = {
            "bits1": qm.TurboQuantBitSize.BITS1,
            "bits1_5": qm.TurboQuantBitSize.BITS1_5,
            "bits2": qm.TurboQuantBitSize.BITS2,
            "bits4": qm.TurboQuantBitSize.BITS4,
        }
        try:
            bits = bits_by_name[self._quantization_bits.lower()]
        except KeyError as e:
            raise IndexerError(
                "unsupported vector_store.quantization.bits: "
                f"{self._quantization_bits}; expected one of "
                f"{', '.join(sorted(bits_by_name))}"
            ) from e
        return qm.TurboQuantization(
            turbo=qm.TurboQuantQuantizationConfig(
                always_ram=self._quantization_always_ram,
                bits=bits,
            )
        )

    def _to_point(
        self,
        ec: EmbeddedChunk,
        sparse: SparseVector | None,
        *,
        source_key: str | None = None,
        content_hash: str | None = None,
        graph_node_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        source_ref: dict | None = None,
        citation_anchor: str | None = None,
        graph_extraction_version: str | None = None,
        artifact_type: str | None = None,
        embedding_version: str | None = None,
        indexed_at: str | None = None,
    ) -> qm.PointStruct:
        c: Chunk = ec.chunk
        vectors: dict[str, object] = {"dense": ec.vector}
        if sparse is not None and sparse.indices:
            vectors["bm25"] = qm.SparseVector(
                indices=sparse.indices, values=sparse.values
            )
        return qm.PointStruct(
            id=c.id,
            vector=vectors,
            payload={
                "product_id": c.product_id,
                "resource_uri": c.resource.uri,
                "source_id": c.resource.source_id,
                "source_key": source_key,
                "content_hash": content_hash,
                "embedding_version": embedding_version,
                "indexed_at": indexed_at,
                "mime": c.resource.mime,
                "kind": c.kind.value,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "context_path": c.context_path,
                "content": c.content,
                "graph_node_ids": graph_node_ids or [],
                "entity_ids": entity_ids or [],
                "source_ref": source_ref or {},
                "citation_anchor": citation_anchor or c.anchor,
                "graph_extraction_version": graph_extraction_version,
                "artifact_type": artifact_type or c.kind.value,
            },
        )
