"""Retrieval pipeline: dense + sparse -> RRF merge -> rerank.

Three stages, no fallbacks beyond rerank-soft-fail. Add complexity only when
an eval set proves it moves the number.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from nexus.config import NexusConfig
from nexus.ingest.embedder import EmbedderClient
from nexus.ingest.indexer import Indexer
from nexus.retrieval.hybrid import Hit, rrf_merge
from nexus.retrieval.reranker import RerankerClient
from nexus.retrieval.sparse import aencode_query

log = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    hits: list[Hit]
    reranked: bool = True
    seed_count: int = 0
    filtered_by_gate: int = 0
    best_score_before_gate: float | None = None


@dataclass
class RetrievalContext:
    embedder: EmbedderClient
    indexer: Indexer
    reranker: RerankerClient
    config: NexusConfig

    @classmethod
    def from_config(cls, config: NexusConfig) -> RetrievalContext:
        emb_url = config.models.embedding.url or "http://localhost:8080"
        rerank_url = config.models.reranker.url or "http://localhost:8081"
        return cls(
            embedder=EmbedderClient(base_url=emb_url),
            indexer=Indexer(url=config.vector_store.url),
            reranker=RerankerClient(base_url=rerank_url),
            config=config,
        )

    async def aclose(self) -> None:
        await self.embedder.aclose()
        await self.reranker.aclose()
        await self.indexer.aclose()


async def retrieve(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query: str,
    top_k: int = 10,
    mode: Literal["auto", "code", "text"] = "auto",
) -> RetrievalResult:
    """Run the pipeline. Caller is responsible for `ctx.aclose()`."""
    if mode == "code":
        vector_kinds = ["code"]
    elif mode == "text":
        vector_kinds = ["text"]
    else:
        vector_kinds = ["code", "text"]

    primary_vec_name = "dense_code" if vector_kinds[0] == "code" else "dense_text"
    query_vector = await ctx.embedder.embed_query(
        query, vector=primary_vec_name  # type: ignore[arg-type]
    )

    seed_set = await _hybrid_search(
        ctx=ctx,
        product_id=product_id,
        query_vector=query_vector,
        sparse_query=query,
        vector_kinds=vector_kinds,
    )

    if not seed_set:
        return RetrievalResult(hits=[], reranked=False, seed_count=0)

    reranked = True
    try:
        rerank_inputs = [_to_doc(h) for h in seed_set]
        ranking = await ctx.reranker.rerank(query, rerank_inputs, top_k=top_k)
        final_hits = [
            Hit(
                id=seed_set[r.index].id,
                score=r.score,
                payload=seed_set[r.index].payload,
                source=seed_set[r.index].source,
            )
            for r in ranking
        ]
    except Exception as e:
        log.warning("rerank failed, falling back to fused order: %s", e)
        final_hits = seed_set[:top_k]
        reranked = False

    gate = ctx.config.ingestion.quality_gate_threshold
    if reranked:
        final_hits, filtered_by_gate, best_score = _apply_quality_gate(final_hits, gate)
    else:
        filtered_by_gate = 0
        best_score = max((h.score for h in final_hits), default=None)

    return RetrievalResult(
        hits=final_hits,
        reranked=reranked,
        seed_count=len(seed_set),
        filtered_by_gate=filtered_by_gate,
        best_score_before_gate=best_score,
    )


async def _hybrid_search(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query_vector: list[float],
    sparse_query: str,
    vector_kinds: list[str],
) -> list[Hit]:
    """Dense + BM25 per modality, then RRF fuse to top-20 seed set."""
    sparse_vec = await aencode_query(sparse_query)

    async def _dense(kind: str) -> list[Hit]:
        name = "dense_code" if kind == "code" else "dense_text"
        raw = await ctx.indexer.search_dense(
            product_id=product_id,
            query_vector=query_vector,
            vector_name=name,
            top_k=50,
        )
        return [
            Hit(id=r["id"], score=r["score"], payload=r["payload"] or {}, source="dense")
            for r in raw
        ]

    async def _sparse(kind: str) -> list[Hit]:
        raw = await ctx.indexer.search_sparse(
            product_id=product_id,
            sparse=sparse_vec,
            vector_kind=kind,
            top_k=50,
        )
        return [
            Hit(id=r["id"], score=r["score"], payload=r["payload"] or {}, source="bm25")
            for r in raw
        ]

    rankings: list[list[Hit]] = []
    for kind in vector_kinds:
        d, s = await asyncio.gather(_dense(kind), _sparse(kind))
        rankings.extend([d, s])
    return rrf_merge(rankings, top_k=20)


def _to_doc(hit: Hit) -> str:
    payload = hit.payload
    anchor = f'{payload.get("resource_uri","?")}:{payload.get("start_line","?")}'
    ctx_path = payload.get("context_path") or ""
    head = f"[{anchor}]" + (f" {ctx_path}" if ctx_path else "")
    body = payload.get("content", "")
    return f"{head}\n{body}"


def _apply_quality_gate(hits: list[Hit], gate: float) -> tuple[list[Hit], int, float | None]:
    best_score = max((h.score for h in hits), default=None)
    kept = [h for h in hits if h.score >= gate]
    return kept, len(hits) - len(kept), best_score
