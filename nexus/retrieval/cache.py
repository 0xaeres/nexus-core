"""Semantic cache — Qdrant ANN against query embeddings.

Threshold 0.92 by default (per nexus.yaml `cache.semantic_threshold`).
Per-product scope: cache entries from product A are never returned to product B.

We store one record per (query+context, product_id) keyed by a deterministic
UUID. The vector is the embedding of `query|context`. The payload stores the
serialised retrieval result so cache hits skip all stages.

Invalidation: per-product `purge(product_id, chunk_ids)` removes any cache
entry whose stored chunk IDs intersect the invalidated set. (Wired up by the
continuous index daemon in Slice 5; the API is here.)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

_CACHE_NS = uuid.UUID("c19b85d4-3a4f-4c8b-9f12-7e2a5d3b1f10")
_CACHE_COLLECTION_DEFAULT = "nexus_cache"


@dataclass(frozen=True)
class CacheHit:
    score: float
    age_s: float
    result: list[dict]


class SemanticCache:
    def __init__(
        self,
        client: AsyncQdrantClient,
        *,
        collection: str = _CACHE_COLLECTION_DEFAULT,
        threshold: float = 0.92,
        ttl_s: int = 24 * 3600,
        vector_dim: int = 2048,
    ):
        self.client = client
        self.collection = collection
        self.threshold = threshold
        self.ttl_s = ttl_s
        self.vector_dim = vector_dim
        self._lock = asyncio.Lock()
        self._ready = False

    async def ensure_collection(self) -> None:
        async with self._lock:
            if self._ready:
                return
            existing = {c.name for c in (await self.client.get_collections()).collections}
            if self.collection not in existing:
                await self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config=qm.VectorParams(
                        size=self.vector_dim, distance=qm.Distance.COSINE
                    ),
                )
                await self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name="product_id",
                    field_schema=qm.PayloadSchemaType.KEYWORD,
                )
            self._ready = True

    # --------------------------------------------------------- read

    async def lookup(
        self, *, product_id: str, query_vector: list[float]
    ) -> CacheHit | None:
        await self.ensure_collection()
        result = await self.client.query_points(
            collection_name=self.collection,
            query=query_vector,
            limit=1,
            score_threshold=self.threshold,
            query_filter=qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="product_id", match=qm.MatchValue(value=product_id)
                    )
                ]
            ),
            with_payload=True,
        )
        if not result.points:
            return None
        pt = result.points[0]
        payload = pt.payload or {}
        created_at = payload.get("created_at", 0.0)
        age = time.time() - float(created_at)
        if age > self.ttl_s:
            return None
        return CacheHit(
            score=pt.score, age_s=age, result=json.loads(payload.get("result", "[]"))
        )

    # --------------------------------------------------------- write

    async def put(
        self,
        *,
        product_id: str,
        query: str,
        context: str,
        query_vector: list[float],
        result: list[dict],
    ) -> None:
        await self.ensure_collection()
        chunk_ids = [r.get("id") for r in result if r.get("id")]
        key = f"{product_id}|{query}|{context}"
        pid = str(uuid.uuid5(_CACHE_NS, key))
        await self.client.upsert(
            collection_name=self.collection,
            points=[
                qm.PointStruct(
                    id=pid,
                    vector=query_vector,
                    payload={
                        "product_id": product_id,
                        "query": query,
                        "context": context,
                        "result": json.dumps(result),
                        "chunk_ids": chunk_ids,
                        "created_at": time.time(),
                    },
                )
            ],
        )

    # --------------------------------------------------------- invalidate

    async def purge(self, *, product_id: str, chunk_ids: list[str]) -> int:
        """Remove cache entries that referenced any of the given chunk_ids."""
        if not chunk_ids:
            return 0
        await self.ensure_collection()
        await self.client.delete(
            collection_name=self.collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="product_id", match=qm.MatchValue(value=product_id)
                        ),
                        qm.FieldCondition(
                            key="chunk_ids",
                            match=qm.MatchAny(any=chunk_ids),
                        ),
                    ]
                )
            ),
        )
        return len(chunk_ids)
