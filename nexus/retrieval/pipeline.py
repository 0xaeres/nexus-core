"""5-stage retrieval orchestrator (spec §5).

  Stage 0  — query classifier + HyDE (for complex queries)
             semantic cache check (0.92 threshold)
  Stage 1  — dense ANN (top-50)
  Stage 2  — sparse BM25 (top-50)
  Stage 3  — RRF merge → seed set (top-20)
  Stage 4  — Neo4j graph expansion (stub until Slice 6)
  Stage 5  — Jina Reranker v3 cross-encoder

Plus quality gate (score < 0.3 → filter; all-filtered → re-query with HyDE
forced; second pass fails → no_context signal) and component circuit breakers
with graceful degradation (Neo4j down → skip Stage 4; reranker down → skip
Stage 5; Qdrant down → 503).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

from nexus.config import NexusConfig
from nexus.graph.store import GraphStore
from nexus.ingest.embedder import EmbedderClient
from nexus.ingest.indexer import Indexer
from nexus.observability.otel import span as otel_span
from nexus.retrieval.cache import CacheHit, SemanticCache
from nexus.retrieval.circuit import CircuitBreaker, CircuitOpen
from nexus.retrieval.classifier import Complexity, classify
from nexus.retrieval.graph import expand as graph_expand
from nexus.retrieval.hybrid import Hit, rrf_merge
from nexus.retrieval.hyde import HydeClient
from nexus.retrieval.reranker import RerankerClient
from nexus.retrieval.sparse import aencode_query

log = logging.getLogger(__name__)


RetrievalMode = Literal["normal", "degraded", "no_context"]


@dataclass
class RetrievalResult:
    hits: list[Hit]
    mode: RetrievalMode
    degraded_components: list[str] = field(default_factory=list)
    cache_hit: bool = False
    used_hyde: bool = False
    classifier_complexity: str = ""
    classifier_reason: str = ""


# ---------------------------------------------------------------- factory


@dataclass
class RetrievalContext:
    """Lazily-constructed handles for one retrieval invocation.

    Sized for short-lived CLI usage. The MCP server / API can keep these
    instances around across calls (see `make_long_lived` below).
    """

    embedder: EmbedderClient
    indexer: Indexer
    cache: SemanticCache
    reranker: RerankerClient
    hyde: HydeClient
    graph: GraphStore
    qdrant_breaker: CircuitBreaker
    reranker_breaker: CircuitBreaker
    graph_breaker: CircuitBreaker
    config: NexusConfig

    @classmethod
    def from_config(cls, config: NexusConfig) -> RetrievalContext:
        emb_url = config.models.embedding.url or "http://localhost:8080"
        rerank_url = config.models.reranker.url or "http://localhost:8081"
        light_url = config.models.light.base_url or "http://localhost:11434"
        embedder = EmbedderClient(base_url=emb_url)
        indexer = Indexer(url=config.vector_store.url)
        cache = SemanticCache(
            client=indexer.client,
            threshold=config.cache.semantic_threshold,
            ttl_s=config.cache.ttl_hours * 3600,
        )
        reranker = RerankerClient(base_url=rerank_url)
        hyde = HydeClient(base_url=light_url, model=config.models.light.model)
        graph = GraphStore(
            url=config.graph.url,
            user=config.graph.user,
            password=config.graph.password,
        )
        cb = config.retrieval.circuit_breaker
        return cls(
            embedder=embedder,
            indexer=indexer,
            cache=cache,
            reranker=reranker,
            hyde=hyde,
            graph=graph,
            qdrant_breaker=CircuitBreaker(
                "qdrant", failure_threshold=cb.failure_threshold,
                recovery_timeout_s=cb.recovery_timeout_s,
            ),
            reranker_breaker=CircuitBreaker(
                "reranker", failure_threshold=cb.failure_threshold,
                recovery_timeout_s=cb.recovery_timeout_s,
            ),
            graph_breaker=CircuitBreaker(
                "neo4j", failure_threshold=cb.failure_threshold,
                recovery_timeout_s=cb.recovery_timeout_s,
            ),
            config=config,
        )

    async def aclose(self) -> None:
        await self.embedder.aclose()
        await self.reranker.aclose()
        await self.hyde.aclose()
        await self.indexer.aclose()
        await self.graph.aclose()


# ---------------------------------------------------------------- entry point


async def retrieve(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query: str,
    top_k: int = 10,
    mode: Literal["auto", "code", "text"] = "auto",
    context_hint: str = "",
) -> RetrievalResult:
    """Run the full pipeline. Caller is responsible for `ctx.aclose()`."""
    async with otel_span(
        "retrieval.query_classify",
        product_id=product_id,
        query_len=len(query),
        mode=mode,
    ) as sp:
        classified = classify(
            query, threshold=ctx.config.retrieval.simple_query_threshold
        )
        sp.set_attribute("complexity", classified.complexity.value)
        sp.set_attribute("confidence", classified.confidence)
    log.debug("classify: %s (%.2f) — %s", classified.complexity, classified.confidence, classified.reason)

    use_hyde = (
        classified.complexity is Complexity.COMPLEX
        and ctx.config.retrieval.hyde_enabled
    )
    return await _run_once(
        ctx=ctx,
        product_id=product_id,
        query=query,
        top_k=top_k,
        mode=mode,
        context_hint=context_hint,
        use_hyde=use_hyde,
        classifier_complexity=classified.complexity.value,
        classifier_reason=classified.reason,
        is_requery=False,
    )


async def _run_once(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query: str,
    top_k: int,
    mode: str,
    context_hint: str,
    use_hyde: bool,
    classifier_complexity: str,
    classifier_reason: str,
    is_requery: bool,
) -> RetrievalResult:
    degraded: list[str] = []

    # ---- pick vector modality(ies) ----
    if mode == "code":
        vector_kinds = ["code"]
    elif mode == "text":
        vector_kinds = ["text"]
    else:
        vector_kinds = ["code", "text"]

    # ---- HyDE-augmented embedding text ----
    augmented_query = query
    if use_hyde:
        async with otel_span("retrieval.hyde", mode=vector_kinds[0]) as sp:
            hyde_text = await ctx.hyde.generate(
                query, mode="code" if "code" in vector_kinds else "text"
            )
            sp.set_attribute("hyde_chars", len(hyde_text or ""))
        if hyde_text:
            augmented_query = f"{query}\n\n{hyde_text}"

    # ---- embed the query (dense, once per modality is fine — same vector) ----
    primary_vec_name = (
        "dense_code" if vector_kinds[0] == "code" else "dense_text"
    )
    async with otel_span("retrieval.embed.query", vector_name=primary_vec_name) as sp:
        query_vector = await ctx.embedder.embed_query(
            augmented_query, vector=primary_vec_name  # type: ignore[arg-type]
        )
        sp.set_attribute("dim", len(query_vector))

    # ---- semantic cache ----
    async with otel_span("retrieval.cache.check", product_id=product_id) as sp:
        try:
            hit: CacheHit | None = await ctx.qdrant_breaker.call(
                ctx.cache.lookup, product_id=product_id, query_vector=query_vector
            )
        except CircuitOpen:
            hit = None
            degraded.append("qdrant-cache")
        except Exception as e:
            log.warning("cache lookup failed: %s", e)
            hit = None
        sp.set_attribute("cache_hit", hit is not None)

    if hit is not None:
        cached_hits = [
            Hit(id=r["id"], score=r["score"], payload=r["payload"], source="cache")
            for r in hit.result
        ]
        return RetrievalResult(
            hits=cached_hits[:top_k],
            mode="normal",
            cache_hit=True,
            used_hyde=use_hyde,
            classifier_complexity=classifier_complexity,
            classifier_reason=classifier_reason,
        )

    # ---- Stages 1-3: dense + BM25 -> RRF ----
    try:
        async with otel_span(
            "retrieval.rrf_merge", vector_kinds=",".join(vector_kinds)
        ) as sp:
            seed_set = await ctx.qdrant_breaker.call(
                _stages_1_through_3,
                ctx=ctx,
                product_id=product_id,
                query_vector=query_vector,
                sparse_query=query,
                vector_kinds=vector_kinds,
            )
            sp.set_attribute("seed_count", len(seed_set))
    except CircuitOpen:
        return RetrievalResult(
            hits=[],
            mode="no_context",
            degraded_components=["qdrant"],
            used_hyde=use_hyde,
            classifier_complexity=classifier_complexity,
            classifier_reason=classifier_reason,
        )

    # ---- Stage 4: graph expansion ----
    async with otel_span("retrieval.neo4j.expand", hops=2) as sp:
        try:
            expanded = await ctx.graph_breaker.call(
                graph_expand,
                product_id=product_id,
                seeds=seed_set,
                hops=2,
                graph=ctx.graph,
            )
            sp.set_attribute("nodes_added", max(0, len(expanded) - len(seed_set)))
        except CircuitOpen:
            expanded = seed_set
            degraded.append("neo4j")
            sp.set_attribute("circuit_open", True)
        except Exception as e:
            log.debug("graph expand soft-failed: %s", e)
            expanded = seed_set
            degraded.append("neo4j")
            sp.set_attribute("error", str(e)[:200])

    # ---- Stage 5: rerank (fallible — fall back to fused order) ----
    final_hits = expanded
    async with otel_span("retrieval.reranker.score", candidates=len(expanded)) as sp:
        try:
            rerank_inputs = [_to_doc(h) for h in expanded]
            ranking = await ctx.reranker_breaker.call(
                ctx.reranker.rerank, query, rerank_inputs, top_k=top_k
            )
            reranked: list[Hit] = []
            for r in ranking:
                base = expanded[r.index]
                reranked.append(
                    Hit(id=base.id, score=r.score, payload=base.payload, source=base.source)
                )
            final_hits = reranked
            sp.set_attribute("min_score", min((r.score for r in ranking), default=0.0))
        except CircuitOpen:
            final_hits = expanded[:top_k]
            degraded.append("reranker")
            sp.set_attribute("circuit_open", True)
        except Exception as e:
            log.warning("rerank failed: %s", e)
            final_hits = expanded[:top_k]
            degraded.append("reranker")
            sp.set_attribute("error", str(e)[:200])

    # ---- quality gate + prompt-injection guard ----
    async with otel_span("retrieval.quality_gate") as sp:
        gate = ctx.config.ingestion.quality_gate_threshold
        kept = [h for h in final_hits if h.score >= gate or "reranker" in degraded]
        before_guard = len(kept)
        kept = _scan_for_injection(kept)
        sp.set_attribute("kept", len(kept))
        sp.set_attribute("filtered_ratio", 1.0 - (len(kept) / max(len(final_hits), 1)))
        sp.set_attribute("guard_modified", before_guard != len(kept))
    if not kept and not is_requery:
        # Re-query once with HyDE forced
        return await _run_once(
            ctx=ctx,
            product_id=product_id,
            query=query,
            top_k=top_k,
            mode=mode,
            context_hint=context_hint,
            use_hyde=True,
            classifier_complexity=classifier_complexity,
            classifier_reason=classifier_reason + "; quality-gate re-query",
            is_requery=True,
        )
    if not kept and is_requery:
        return RetrievalResult(
            hits=[],
            mode="no_context",
            degraded_components=degraded,
            used_hyde=True,
            classifier_complexity=classifier_complexity,
            classifier_reason=classifier_reason,
        )

    # ---- cache the result ----
    try:
        await ctx.cache.put(
            product_id=product_id,
            query=query,
            context=context_hint,
            query_vector=query_vector,
            result=[
                {"id": h.id, "score": h.score, "payload": h.payload} for h in kept
            ],
        )
    except Exception as e:
        log.debug("cache put failed: %s", e)

    return RetrievalResult(
        hits=kept,
        mode="degraded" if degraded else "normal",
        degraded_components=degraded,
        used_hyde=use_hyde,
        classifier_complexity=classifier_complexity,
        classifier_reason=classifier_reason,
    )


async def _stages_1_through_3(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query_vector: list[float],
    sparse_query: str,
    vector_kinds: list[str],
) -> list[Hit]:
    """Stage 1 (dense) + Stage 2 (BM25) per modality, then Stage 3 RRF."""
    sparse_vec = await aencode_query(sparse_query)

    async def _dense(kind: str) -> list[Hit]:
        name = "dense_code" if kind == "code" else "dense_text"
        raw = await ctx.indexer.search_dense(
            product_id=product_id,
            query_vector=query_vector,
            vector_name=name,
            top_k=50,
        )
        return [Hit(id=r["id"], score=r["score"], payload=r["payload"] or {}, source="dense") for r in raw]

    async def _sparse(kind: str) -> list[Hit]:
        raw = await ctx.indexer.search_sparse(
            product_id=product_id,
            sparse=sparse_vec,
            vector_kind=kind,
            top_k=50,
        )
        return [Hit(id=r["id"], score=r["score"], payload=r["payload"] or {}, source="bm25") for r in raw]

    rankings: list[list[Hit]] = []
    for kind in vector_kinds:
        d, s = await asyncio.gather(_dense(kind), _sparse(kind))
        rankings.extend([d, s])
    return rrf_merge(rankings, top_k=20)


def _to_doc(hit: Hit) -> str:
    """Render a retrieved chunk as the document text passed to the reranker."""
    payload = hit.payload
    anchor = f'{payload.get("resource_uri","?")}:{payload.get("start_line","?")}'
    ctx_path = payload.get("context_path") or ""
    head = f"[{anchor}]" + (f" {ctx_path}" if ctx_path else "")
    body = payload.get("content", "")
    return f"{head}\n{body}"


def _scan_for_injection(hits: list[Hit]) -> list[Hit]:
    """Run the prompt-injection guard on each kept hit and redact content
    where adversarial patterns appear. We never *drop* hits - reviewers may
    still want to see the citation - we only swap the body for a notice so
    downstream agents don't ingest the malicious text."""
    from nexus.retrieval.guard import scan_payloads

    if not hits:
        return hits
    payloads = [dict(h.payload or {}) for h in hits]
    safe, all_hits = scan_payloads(payloads)
    if not all_hits:
        return hits
    return [
        Hit(id=h.id, score=h.score, payload=safe[i], source=h.source)
        for i, h in enumerate(hits)
    ]
