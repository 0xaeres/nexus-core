"""Qdrant indexer — named vectors + per-product shard key isolation (§12).

Two collections (per nexus.yaml `vector_store.collections`):

  nexus_code   stores code chunks; named vectors: {dense, bm25}
  nexus_text   stores doc chunks;  named vectors: {dense, bm25}

`dense`  — Jina v4 (JINA_V4_DIM, cosine)
`bm25`   — fastembed Qdrant/bm25 sparse encoder; Qdrant applies IDF server-side

Both collections use `sharding_method: custom` with `product_id` as the shard
key — queries that omit a `product_id` filter return nothing by construction.
"""

from __future__ import annotations

from collections.abc import Sequence

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from nexus.ingest.models import Chunk, EmbeddedChunk
from nexus.retrieval.sparse import SparseVector

# Default Jina v4 embedding dimensionality. Configurable per-collection on create.
JINA_V4_DIM = 2048

_CODE_COLLECTION = "nexus_code"
_TEXT_COLLECTION = "nexus_text"


class IndexerError(RuntimeError):
    pass


class Indexer:
    """Async Qdrant wrapper. Construct once per process."""

    def __init__(
        self,
        url: str = "http://localhost:6333",
        *,
        code_collection: str = _CODE_COLLECTION,
        text_collection: str = _TEXT_COLLECTION,
        vector_dim: int = JINA_V4_DIM,
    ):
        self.client = AsyncQdrantClient(url=url)
        self._code = code_collection
        self._text = text_collection
        self._dim = vector_dim

    async def aclose(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------ setup

    async def ensure_collections(self) -> None:
        """Idempotent: create collections with named vectors + custom shard key."""
        existing = {c.name for c in (await self.client.get_collections()).collections}
        for name in (self._code, self._text):
            if name in existing:
                continue
            await self.client.create_collection(
                collection_name=name,
                vectors_config={
                    "dense": qm.VectorParams(
                        size=self._dim,
                        distance=qm.Distance.COSINE,
                        hnsw_config=qm.HnswConfigDiff(m=16, ef_construct=128),
                    ),
                },
                sparse_vectors_config={
                    "bm25": qm.SparseVectorParams(
                        modifier=qm.Modifier.IDF,
                    ),
                },
                sharding_method=qm.ShardingMethod.CUSTOM,
            )
            await self.client.create_payload_index(
                collection_name=name,
                field_name="product_id",
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
            await self.client.create_payload_index(
                collection_name=name,
                field_name="resource_uri",
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )

    # ------------------------------------------------------------ write

    async def upsert(
        self,
        embedded: Sequence[EmbeddedChunk],
        *,
        sparse_by_id: dict[str, SparseVector] | None = None,
    ) -> int:
        """Upsert dense + (optional) sparse vectors per chunk."""
        if not embedded:
            return 0
        sparse_by_id = sparse_by_id or {}
        buckets: dict[tuple[str, str], list[qm.PointStruct]] = {}
        for ec in embedded:
            coll = self._code if ec.vector_name == "dense_code" else self._text
            point = self._to_point(ec, sparse_by_id.get(ec.chunk.id))
            buckets.setdefault((coll, ec.chunk.product_id), []).append(point)

        n = 0
        for (coll, product_id), points in buckets.items():
            await self.client.upsert(
                collection_name=coll,
                points=points,
                shard_key_selector=product_id,
            )
            n += len(points)
        return n

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
        sparse: SparseVector,
        vector_kind: str,
        top_k: int = 50,
    ) -> list[dict]:
        """Sparse BM25 search. `vector_kind` is 'code' or 'text'."""
        coll = self._code if vector_kind == "code" else self._text
        return await self._search(
            collection=coll,
            product_id=product_id,
            query=qm.SparseVector(indices=sparse.indices, values=sparse.values),
            using="bm25",
            top_k=top_k,
        )

    async def _search(
        self,
        *,
        collection: str,
        product_id: str,
        query,
        using: str,
        top_k: int,
    ) -> list[dict]:
        result = await self.client.query_points(
            collection_name=collection,
            query=query,
            using=using,
            limit=top_k,
            shard_key_selector=product_id,
            query_filter=qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="product_id", match=qm.MatchValue(value=product_id)
                    )
                ]
            ),
            with_payload=True,
        )
        return [
            {"id": pt.id, "score": pt.score, "payload": pt.payload}
            for pt in result.points
        ]

    async def count(self, *, product_id: str, vector_kind: str) -> int:
        coll = self._code if vector_kind == "code" else self._text
        res = await self.client.count(
            collection_name=coll,
            count_filter=qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="product_id", match=qm.MatchValue(value=product_id)
                    )
                ]
            ),
            exact=True,
        )
        return res.count

    async def delete_by_resource(
        self, *, product_id: str, resource_uri: str
    ) -> list[str]:
        """Delete all points for a resource. Returns the deleted IDs for cache
        invalidation hooks."""
        deleted: list[str] = []
        for coll in (self._code, self._text):
            # Scroll for the IDs before we delete, since `delete` doesn't echo them.
            scrolled, _ = await self.client.scroll(
                collection_name=coll,
                scroll_filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="product_id", match=qm.MatchValue(value=product_id)
                        ),
                        qm.FieldCondition(
                            key="resource_uri", match=qm.MatchValue(value=resource_uri)
                        ),
                    ]
                ),
                limit=1024,
                with_payload=False,
                with_vectors=False,
            )
            if not scrolled:
                continue
            ids = [str(pt.id) for pt in scrolled]
            deleted.extend(ids)
            await self.client.delete(
                collection_name=coll,
                points_selector=qm.PointIdsList(points=[pt.id for pt in scrolled]),
                shard_key_selector=product_id,
            )
        return deleted

    # ------------------------------------------------------------ helpers

    def _to_point(
        self, ec: EmbeddedChunk, sparse: SparseVector | None
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
                "mime": c.resource.mime,
                "kind": c.kind.value,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "context_path": c.context_path,
                "content": c.content,
            },
        )
