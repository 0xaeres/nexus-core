"""Evidence-oriented retrieval over hybrid, exact, repo-map, graph, and skills.

`retrieve()` remains the low-level dense + BM25 + rerank primitive. This module
assembles a broader evidence set for product understanding tasks where the right
answer often needs exact symbols, docs, graph-connected chunks, and approved
skills in addition to semantic matches.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from anvay.retrieval.chunk_grep import grep_indexed_chunks
from anvay.retrieval.hybrid import Hit
from anvay.retrieval.pipeline import RetrievalContext, retrieve
from anvay.retrieval.repomap import Symbol, load_repo_map_for_product, topic_bias_terms
from anvay.skills.models import Skill

QueryShape = Literal["local", "global", "relational", "procedural"]
EvidenceMode = Literal["auto", "local", "global", "drift_lite"]
EvidenceChannel = Literal["hybrid", "grep", "repo_map", "graph", "summary", "skill"]
EvidenceRole = Literal[
    "overview",
    "definition",
    "implementation",
    "relationship",
    "validation",
    "skill_guidance",
]

_PATH_RE = re.compile(
    r"\b[\w./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|md|mdx|sql|yaml|yml|toml|json)\b"
)
_ROUTE_RE = re.compile(r"/[A-Za-z0-9_./:{}-]+")
_SYMBOL_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_CONFIG_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
# A bare word is treated as a code symbol only when it carries an internal code
# signal — snake_case, internal camel/Pascal case, or a call ``foo()`` — so NL
# stopwords (sentence-initial "How"/"What") never masquerade as anchors and dead
# the graph-local channel. See plan Phase 1b.
_CAMEL_RE = re.compile(r"[a-z][A-Z]|[A-Z][a-z].*[A-Z]")
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
# Common interrogatives / sentence starters that capitalize but are not symbols.
_SYMBOL_STOPWORDS = {
    "how",
    "what",
    "why",
    "when",
    "where",
    "which",
    "who",
    "does",
    "did",
    "can",
    "the",
    "this",
    "that",
    "these",
    "those",
    "and",
    "but",
    "for",
    "are",
    "is",
    "was",
}
_LOCAL_WORDS = {
    "where",
    "defined",
    "definition",
    "symbol",
    "constant",
    "function",
    "class",
    "file",
    "route",
}
_GLOBAL_WORDS = {
    "architecture",
    "strategy",
    "pipeline",
    "overview",
    "explain",
    "how",
    "flow",
    "design",
    "system",
}
_RELATIONAL_WORDS = {
    "uses",
    "use",
    "depends",
    "dependency",
    "impact",
    "calls",
    "called",
    "owns",
    "reads",
    "writes",
    "connects",
}
_PROCEDURAL_WORDS = {
    "should",
    "change",
    "debug",
    "review",
    "implement",
    "fix",
    "test",
}


class QueryUnderstanding(BaseModel):
    query: str
    shape: QueryShape
    anchors: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    config_keys: list[str] = Field(default_factory=list)
    facets: list[str] = Field(default_factory=list)


class RetrievalTrace(BaseModel):
    channel: EvidenceChannel | str
    query: str
    product_id: str = ""
    hits: int = 0
    detail: str = ""
    latency_ms: float = 0.0


class EvidenceCandidate(BaseModel):
    chunk_id: str
    channel: EvidenceChannel
    role: EvidenceRole
    score: float = 0.0
    file: str = ""
    line: int = 0
    end_line: int | None = None
    context_path: str | None = None
    excerpt: str = ""
    graph_node_ids: list[str] = Field(default_factory=list)
    graph_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def anchor(self) -> str:
        return f"{self.file}:{self.line}" if self.file else self.chunk_id


class EvidenceCoverage(BaseModel):
    sufficient: bool
    missing_facets: list[str] = Field(default_factory=list)
    covered_facets: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class QueryPlan(BaseModel):
    mode: EvidenceMode
    shape: QueryShape
    channels_run: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)
    graph_seed_strategy: str = "none"
    strategy: str = "hybrid_graph"
    seed_entities: list[str] = Field(default_factory=list)
    edge_types: list[str] = Field(default_factory=list)
    community_hits: int = 0
    followups: list[str] = Field(default_factory=list)
    graph_paths: list[str] = Field(default_factory=list)
    graph_entity_count: int = 0
    graph_relationship_count: int = 0
    graph_relationships_used: bool = False
    graph_diagnostics: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    coverage: EvidenceCoverage | None = None
    fallbacks: list[str] = Field(default_factory=list)
    budget_ms: float | None = None
    budget_exceeded: bool = False
    latency_ms: float = 0.0


class EvidenceSet(BaseModel):
    product_id: str
    query: str
    understanding: QueryUnderstanding
    candidates: list[EvidenceCandidate] = Field(default_factory=list)
    coverage: EvidenceCoverage
    trace: list[RetrievalTrace] = Field(default_factory=list)
    query_plan: QueryPlan | None = None
    reranked: bool = False


async def retrieve_evidence(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query: str,
    top_k: int = 10,
    mode: Literal["auto", "code", "text"] = "auto",
    graph_store: object | None = None,
    skills: Iterable[Skill] | None = None,
    current_file: str | None = None,
    max_depth: int = 2,
    query_mode: EvidenceMode = "auto",
    budget_ms: float | None = None,
) -> EvidenceSet:
    """Retrieve a coverage-oriented evidence set for product/code questions.

    `budget_ms` is a soft deadline. The core fan-out always runs; the optional
    enrichment stages (DRIFT-lite follow-ups and coverage repair) are skipped
    once the budget is spent, returning best-effort evidence rather than
    blocking on the long tail. `None` (default) keeps the unbounded behavior.
    """
    started = time.perf_counter()
    understanding = understand_query(query, current_file=current_file)
    plan = _query_plan(understanding, query_mode=query_mode)
    plan.budget_ms = budget_ms
    trace: list[RetrievalTrace] = []

    def _over_budget() -> bool:
        if budget_ms is None:
            return False
        return (time.perf_counter() - started) * 1000 >= budget_ms

    hybrid_task = hybrid_candidates(
        ctx=ctx,
        product_id=product_id,
        query=query,
        top_k=max(top_k * 4, 20),
        mode=mode,
    )
    grep_task = grep_candidates(
        ctx=ctx,
        product_id=product_id,
        query=_grep_query(understanding),
        limit=max(top_k, 12),
    )
    repo_task = repo_map_candidates(
        ctx=ctx,
        product_id=product_id,
        query=query,
        limit=8,
    )
    graph_task = graph_local_candidates(
        ctx=ctx,
        graph_store=graph_store,
        product_id=product_id,
        understanding=understanding,
        max_depth=max_depth,
        limit=max(top_k, 12),
    )
    summary_task = summary_candidates(
        ctx=ctx,
        product_id=product_id,
        query=query,
        limit=6 if plan.mode in {"global", "drift_lite"} else 3,
    )
    skill_task = skill_candidates(
        product_id=product_id,
        query=query,
        skills=skills or [],
        limit=4,
    )

    results = await asyncio.gather(
        hybrid_task,
        grep_task,
        repo_task,
        graph_task,
        summary_task,
        skill_task,
        return_exceptions=True,
    )
    channels: list[list[EvidenceCandidate]] = []
    reranked = False
    for name, result in zip(
        ("hybrid", "grep", "repo_map", "graph", "summary", "skill"),
        results,
        strict=True,
    ):
        if isinstance(result, Exception):
            trace.append(RetrievalTrace(channel=name, query=query, hits=0, detail=str(result)))
            continue
        candidates, channel_trace, channel_reranked = result
        channels.append(candidates)
        trace.extend(channel_trace)
        reranked = reranked or channel_reranked
    plan.channels_run = _ordered_unique(str(item.channel) for item in trace)

    pooled = [candidate for group in channels for candidate in group]
    plan.seed_entities = _ordered_unique(
        gid for candidate in pooled for gid in candidate.graph_node_ids
    )[:24]
    plan.community_hits = sum(
        1
        for candidate in pooled
        if candidate.metadata.get("artifact_type") == "graph_community_summary"
    )
    if plan.mode == "drift_lite" and _over_budget():
        plan.budget_exceeded = True
        plan.fallbacks.append("latency_budget_skipped_drift_lite")
    elif plan.mode == "drift_lite":
        plan.followups = _drift_followup_queries(query, pooled)
        drifted, drift_trace, drift_reranked = await drift_lite_candidates(
            ctx=ctx,
            product_id=product_id,
            query=query,
            seeds=pooled,
            followups=plan.followups,
            top_k=max(top_k, 8),
        )
        pooled.extend(drifted)
        trace.extend(drift_trace)
        reranked = reranked or drift_reranked
        plan.channels_run = _ordered_unique([*plan.channels_run, "drift_lite"])
        # Reflect actual budget state after the stage ran, not just pre-skip decisions.
        if _over_budget():
            plan.budget_exceeded = True
    pooled, mixed_reranked = await rerank_mixed_candidates(ctx=ctx, query=query, candidates=pooled)
    reranked = reranked or mixed_reranked
    if mixed_reranked:
        trace.append(
            RetrievalTrace(channel="mixed_rerank", query=query, hits=len(pooled))
        )
    merged = merge_candidates(pooled, understanding=understanding, top_k=top_k)
    coverage = assess_coverage(understanding, merged)
    if not coverage.sufficient and _over_budget():
        plan.budget_exceeded = True
        plan.fallbacks.append("latency_budget_skipped_coverage_repair")
    elif not coverage.sufficient:
        plan.fallbacks.append("coverage_repair")
        repaired, repair_trace = await repair_missing_facets(
            ctx=ctx,
            product_id=product_id,
            understanding=understanding,
            existing=merged,
            missing=coverage.missing_facets,
            top_k=top_k,
        )
        trace.extend(repair_trace)
        if repaired:
            # Coverage-repair emits raw-scored grep candidates (match counts in the
            # tens). Rerank the combined pool so every candidate shares the 0-1
            # reranker scale before merge — otherwise raw grep scores dominate
            # ``_candidate_rank`` and bury the substantive reranked chunks (P3).
            combined, repair_reranked = await rerank_mixed_candidates(
                ctx=ctx, query=query, candidates=[*merged, *repaired]
            )
            reranked = reranked or repair_reranked
            merged = merge_candidates(combined, understanding=understanding, top_k=top_k)
            coverage = assess_coverage(understanding, merged)
        # Reflect actual budget state after the stage ran, not just pre-skip decisions.
        if _over_budget():
            plan.budget_exceeded = True
    plan.coverage = coverage
    plan.graph_paths = _ordered_unique(
        str(candidate.metadata.get("graph_path") or "")
        for candidate in merged
        if candidate.metadata.get("graph_path")
    )[:8]
    plan.graph_entity_count = len(
        _ordered_unique(gid for candidate in merged for gid in candidate.graph_node_ids)
    )
    plan.graph_relationship_count = max(
        (int(candidate.metadata.get("graph_edge_count") or 0) for candidate in merged),
        default=0,
    )
    plan.graph_relationships_used = plan.graph_relationship_count > 0
    plan.graph_diagnostics = _ordered_unique(
        str(candidate.metadata.get("graph_diagnostic") or "")
        for candidate in merged
        if candidate.metadata.get("graph_diagnostic")
    )[:8]
    if not plan.seed_entities and plan.mode in {"local", "drift_lite"}:
        plan.unknowns.append("no graph seed entities resolved")
    plan.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    plan.channels_run = _ordered_unique(str(item.channel) for item in trace)
    for item in trace:
        item.product_id = product_id
    trace.append(
        RetrievalTrace(
            channel="query_plan",
            query=query,
            product_id=product_id,
            hits=len(merged),
            detail=plan.model_dump_json(),
            latency_ms=plan.latency_ms,
        )
    )

    return EvidenceSet(
        product_id=product_id,
        query=query,
        understanding=understanding,
        candidates=merged,
        coverage=coverage,
        trace=trace,
        query_plan=plan,
        reranked=reranked,
    )


def _query_plan(understanding: QueryUnderstanding, *, query_mode: EvidenceMode) -> QueryPlan:
    mode = query_mode
    if mode == "auto":
        if understanding.shape in {"local", "relational"} or understanding.anchors:
            mode = "local"
        else:
            mode = "global"
    if mode == "drift_lite":
        seed_strategy = "summary_repo_map_followups"
        strategy = "drift_lite"
    elif mode == "local":
        seed_strategy = "explicit_anchor_graph_traversal" if understanding.anchors else "hybrid_hit_graph_seed"
        strategy = "local_graph"
    else:
        seed_strategy = "structural_summary_repo_map"
        strategy = "global_community"
    return QueryPlan(
        mode=mode,
        shape=understanding.shape,
        anchors=understanding.anchors,
        graph_seed_strategy=seed_strategy,
        strategy=strategy,
        edge_types=_edge_types_for(understanding),
    )


def _is_symbol(token: str, called: set[str]) -> bool:
    """A token is a real code anchor only with an internal code signal: a call
    ``foo()``, snake_case, or internal camel/Pascal case. Bare interrogatives
    and capitalized sentence-starters (in ``_SYMBOL_STOPWORDS``) are rejected so
    NL queries don't emit junk anchors that strand the graph-local channel."""
    if token.lower() in _SYMBOL_STOPWORDS:
        return False
    if token.lower() in called:
        return True
    if "_" in token:
        return True
    return bool(_CAMEL_RE.search(token))


def understand_query(query: str, *, current_file: str | None = None) -> QueryUnderstanding:
    lower = query.lower()
    tokens = set(_SYMBOL_RE.findall(lower))
    paths = _ordered_unique([*( _PATH_RE.findall(query)), *([current_file] if current_file else [])])
    routes = _ordered_unique(_ROUTE_RE.findall(query))
    config_keys = _ordered_unique(_CONFIG_RE.findall(query))
    called = {m.lower() for m in _CALL_RE.findall(query)}
    symbols = _ordered_unique(
        token
        for token in _SYMBOL_RE.findall(query)
        if _is_symbol(token, called) and token not in config_keys
    )
    if tokens & _RELATIONAL_WORDS:
        shape: QueryShape = "relational"
    elif tokens & _PROCEDURAL_WORDS:
        shape = "procedural"
    elif paths or routes or symbols or tokens & _LOCAL_WORDS:
        shape = "local"
    elif tokens & _GLOBAL_WORDS:
        shape = "global"
    else:
        shape = "global"

    facets = ["source"]
    if shape == "global":
        facets.extend(["overview", "implementation"])
    if shape == "relational":
        facets.extend(["relationship", "implementation"])
    if paths or symbols or routes or config_keys:
        facets.append("definition")
    if "test" in lower or "validate" in lower:
        facets.append("validation")
    return QueryUnderstanding(
        query=query,
        shape=shape,
        anchors=_ordered_unique([*paths, *routes, *symbols, *config_keys]),
        paths=paths,
        symbols=symbols,
        routes=routes,
        config_keys=config_keys,
        facets=_ordered_unique(facets),
    )


async def hybrid_candidates(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query: str,
    top_k: int,
    mode: Literal["auto", "code", "text"],
) -> tuple[list[EvidenceCandidate], list[RetrievalTrace], bool]:
    result = await retrieve(ctx=ctx, product_id=product_id, query=query, top_k=top_k, mode=mode)
    return (
        [_candidate_from_hit(hit, channel="hybrid") for hit in result.hits],
        [RetrievalTrace(channel="hybrid", query=query, hits=len(result.hits))],
        result.reranked,
    )


async def grep_candidates(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query: str,
    limit: int,
) -> tuple[list[EvidenceCandidate], list[RetrievalTrace], bool]:
    if not hasattr(ctx, "indexer"):
        return (
            [],
            [RetrievalTrace(channel="grep", query=query, hits=0, detail="indexer unavailable")],
            False,
        )
    hits = await grep_indexed_chunks(
        indexer=ctx.indexer,
        product_id=product_id,
        query=query,
        limit=limit,
    )
    out = [
        EvidenceCandidate(
            chunk_id=hit.chunk_id,
            channel="grep",
            role=_role_for(file=hit.file, context_path=None, content=hit.excerpt, source="grep"),
            score=hit.score,
            file=hit.file,
            line=hit.line,
            excerpt=hit.excerpt,
        )
        for hit in hits
    ]
    return out, [RetrievalTrace(channel="grep", query=query, hits=len(out))], False


async def repo_map_candidates(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query: str,
    limit: int,
) -> tuple[list[EvidenceCandidate], list[RetrievalTrace], bool]:
    if not hasattr(ctx, "config"):
        return (
            [],
            [RetrievalTrace(channel="repo_map", query=query, hits=0, detail="config unavailable")],
            False,
        )
    repo_map = await asyncio.to_thread(load_repo_map_for_product, ctx.config, product_id)
    terms = topic_bias_terms(query)
    scored = _score_symbols(repo_map.symbols, terms)
    out = [_candidate_from_symbol(score, symbol) for score, symbol in scored[:limit]]
    return out, [RetrievalTrace(channel="repo_map", query=query, hits=len(out))], False


async def graph_local_candidates(
    *,
    ctx: RetrievalContext,
    graph_store: object | None,
    product_id: str,
    understanding: QueryUnderstanding,
    max_depth: int,
    limit: int,
) -> tuple[list[EvidenceCandidate], list[RetrievalTrace], bool]:
    if graph_store is None or not understanding.anchors:
        return [], [RetrievalTrace(channel="graph", query=understanding.query, hits=0)], False
    if not hasattr(ctx, "indexer"):
        return [], [RetrievalTrace(channel="graph", query=understanding.query, hits=0, detail="indexer unavailable")], False
    if not hasattr(graph_store, "resolve_entity") or not hasattr(graph_store, "traverse"):
        return [], [RetrievalTrace(channel="graph", query=understanding.query, hits=0, detail="graph store lacks query methods")], False

    nodes = []
    for anchor in understanding.anchors[:8]:
        result = await graph_store.resolve_entity(product_id=product_id, mention=anchor, limit=4)
        nodes.extend(getattr(result, "nodes", []) or [])
    seed_ids = _ordered_unique(getattr(node, "stable_id", "") for node in nodes if getattr(node, "stable_id", ""))
    if not seed_ids:
        return [], [RetrievalTrace(channel="graph", query=" ".join(understanding.anchors), hits=0)], False

    edge_types = _edge_types_for(understanding)
    traversal = await graph_store.traverse(
        product_id=product_id,
        seed_ids=seed_ids[:8],
        edge_types=edge_types,
        max_depth=max(1, min(max_depth, 4)),
        limit=80,
    )
    graph_ids = _ordered_unique(
        [
            *seed_ids,
            *[
                getattr(node, "stable_id", "")
                for node in (getattr(traversal, "nodes", []) or [])
                if getattr(node, "stable_id", "")
            ],
        ]
    )
    batches = await asyncio.gather(
        *[
            ctx.indexer.search_by_graph_nodes(
                product_id=product_id,
                graph_node_ids=graph_ids[:40],
                vector_kind=kind,
                top_k=limit,
            )
            for kind in ("code", "text")
        ]
    )
    raw = [item for batch in batches for item in batch]
    traversal_nodes = getattr(traversal, "nodes", []) or []
    traversal_edges = getattr(traversal, "edges", []) or []
    traversal_paths = getattr(traversal, "paths", []) or []
    rank_by_id = _graph_rank_by_id(
        seed_ids=seed_ids,
        nodes=traversal_nodes,
        edges=traversal_edges,
    )
    raw.sort(
        key=lambda item: _graph_hit_rank(item, rank_by_id),
        reverse=True,
    )
    out = []
    for item in raw[:limit]:
        candidate = _candidate_from_hit(
            Hit(id=str(item["id"]), score=float(item.get("score") or 1.0), payload=item.get("payload") or {}, source="graph"),
            channel="graph",
        )
        candidate.score = _graph_hit_rank(item, rank_by_id)
        candidate.metadata["graph_seed_ids"] = seed_ids[:8]
        candidate.metadata["edge_types"] = edge_types
        candidate.metadata["graph_edge_count"] = len(traversal_edges)
        candidate.metadata["graph_relationship_types"] = sorted({getattr(edge, "type", "") for edge in traversal_edges if getattr(edge, "type", "")})
        if traversal_edges:
            candidate.metadata["graph_path"] = _graph_path_label(traversal_paths, candidate.graph_node_ids)
        else:
            candidate.metadata["graph_diagnostic"] = "no graph relationships returned"
        out.append(candidate)
    detail = "" if traversal_edges else "no graph relationships returned"
    return out, [RetrievalTrace(channel="graph", query=" ".join(understanding.anchors), hits=len(out), detail=detail)], False


async def summary_candidates(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query: str,
    limit: int,
) -> tuple[list[EvidenceCandidate], list[RetrievalTrace], bool]:
    if limit <= 0:
        return [], [RetrievalTrace(channel="summary", query=query, hits=0)], False
    result = await retrieve(
        ctx=ctx,
        product_id=product_id,
        query=query,
        top_k=max(limit * 4, 12),
        mode="text",
    )
    out = [
        _candidate_from_hit(hit, channel="summary")
        for hit in result.hits
        if (hit.payload or {}).get("artifact_type") in {"summary", "graph_community_summary"}
    ][:limit]
    return out, [RetrievalTrace(channel="summary", query=query, hits=len(out))], result.reranked


async def skill_candidates(
    *,
    product_id: str,
    query: str,
    skills: Iterable[Skill],
    limit: int,
) -> tuple[list[EvidenceCandidate], list[RetrievalTrace], bool]:
    q_terms = {t for t in topic_bias_terms(query) if len(t) >= 3}
    scored: list[tuple[float, Skill]] = []
    for skill in skills:
        if skill.product != product_id:
            continue
        haystack = f"{skill.name} {skill.description} {skill.body}".lower()
        overlap = sum(1 for term in q_terms if term in haystack)
        score = overlap + (0.25 * skill.confidence)
        if overlap or skill.tier == "product_master":
            scored.append((score, skill))
    scored.sort(key=lambda item: item[0], reverse=True)
    out = [
        EvidenceCandidate(
            chunk_id=f"skill:{skill.id}",
            channel="skill",
            role="skill_guidance",
            score=score,
            file=f"skills/{skill.product}/{skill.name}/SKILL.md",
            line=1,
            excerpt=_truncate(skill.description or _first_paragraph(skill.body), 700),
            metadata={"skill_id": skill.id, "tier": skill.tier, "confidence": skill.confidence},
        )
        for score, skill in scored[:limit]
    ]
    return out, [RetrievalTrace(channel="skill", query=query, hits=len(out))], False


async def drift_lite_candidates(
    *,
    ctx: RetrievalContext,
    product_id: str,
    query: str,
    seeds: Sequence[EvidenceCandidate],
    top_k: int,
    followups: Sequence[str] | None = None,
) -> tuple[list[EvidenceCandidate], list[RetrievalTrace], bool]:
    """Deterministic broad-to-specific follow-up retrieval.

    This is deliberately not HyDE: no synthetic answer is generated. Follow-up
    queries come only from structural summaries, repo-map symbols, and the
    original user query.
    """
    followups = list(followups or _drift_followup_queries(query, seeds))
    out: list[EvidenceCandidate] = []
    trace: list[RetrievalTrace] = []
    reranked = False
    seen: set[tuple[str, int, str]] = set()
    for followup in followups:
        result = await retrieve(
            ctx=ctx,
            product_id=product_id,
            query=followup,
            top_k=max(4, min(top_k, 12)),
            mode="auto",
        )
        reranked = reranked or result.reranked
        candidates = [_candidate_from_hit(hit, channel="hybrid") for hit in result.hits]
        for candidate in candidates:
            key = (candidate.file, candidate.line, candidate.chunk_id)
            if key in seen:
                continue
            seen.add(key)
            candidate.metadata["drift_query"] = followup
            out.append(candidate)
        trace.append(
            RetrievalTrace(channel="drift_lite", query=followup, hits=len(candidates))
        )
    return out, trace, reranked


async def rerank_mixed_candidates(
    *,
    ctx: RetrievalContext,
    query: str,
    candidates: list[EvidenceCandidate],
) -> tuple[list[EvidenceCandidate], bool]:
    if not candidates or not hasattr(ctx, "reranker"):
        return candidates, False
    try:
        docs = [
            f"[{candidate.anchor}] {candidate.role} {candidate.channel}\n{candidate.excerpt}"
            for candidate in candidates
        ]
        ranking = await ctx.reranker.rerank(query, docs, top_k=len(docs))
    except Exception:
        return candidates, False
    reranked: list[EvidenceCandidate] = []
    for item in ranking:
        candidate = candidates[item.index].model_copy(deep=True)
        candidate.score = item.score
        candidate.metadata["mixed_reranked"] = True
        reranked.append(candidate)
    return reranked, True


def merge_candidates(
    candidates: list[EvidenceCandidate],
    *,
    understanding: QueryUnderstanding,
    top_k: int,
) -> list[EvidenceCandidate]:
    deduped: dict[tuple[str, str, int], EvidenceCandidate] = {}
    for candidate in candidates:
        key = (candidate.file, candidate.chunk_id, candidate.line)
        existing = deduped.get(key)
        if existing is None or _channel_weight(candidate.channel) + candidate.score > _channel_weight(existing.channel) + existing.score:
            deduped[key] = candidate

    # Filename-anchor boost: when a query anchor *exactly* names a file (stem ==
    # anchor, e.g. "ImmutableMap" -> ImmutableMap.java), float that file's chunks
    # up. Otherwise the canonical class loses to its many same-prefix impls
    # (RegularImmutableMap/JdkBackedImmutableMap…) on "what is X / X vs Y" queries.
    anchors = {a.lower() for a in understanding.anchors}
    if anchors:
        for candidate in deduped.values():
            if _basename_stem(candidate.file) in anchors:
                candidate.metadata["filename_anchor_match"] = True

    ordered = sorted(deduped.values(), key=_candidate_rank, reverse=True)
    # Quota picks, deduped by key (overview-role take can overlap a channel take).
    selected: list[EvidenceCandidate] = []
    seen: set[tuple[str, str, int]] = set()

    def _add(cands: list[EvidenceCandidate]) -> None:
        for c in cands:
            key = (c.file, c.chunk_id, c.line)
            if key not in seen:
                seen.add(key)
                selected.append(c)

    # Hybrid is the primary content channel — reserve it the largest share *first*
    # so substantive dense chunks are never crowded out by the auxiliary quotas
    # (grep/repo_map/summary/overview), which previously starved hybrid to ~1 slot
    # on global queries and tanked context_recall (esp. large Java files).
    _add(_take_channel(ordered, "hybrid", max(3, top_k // 2)))
    _add(_take_channel(ordered, "grep", 2 if understanding.anchors else 1))
    _add(_take_channel(ordered, "repo_map", 2 if understanding.shape in {"global", "local"} else 1))
    _add(_take_channel(ordered, "graph", 3 if understanding.shape == "relational" else 1))
    _add(_take_channel(ordered, "summary", 2 if understanding.shape == "global" else 1))
    _add(_take_role(ordered, "overview", 2 if understanding.shape == "global" else 1))
    _add(_take_channel(ordered, "skill", 1 if understanding.shape in {"procedural", "global"} else 0))

    per_file: dict[str, int] = {}
    for candidate in selected:
        per_file[candidate.file] = per_file.get(candidate.file, 0) + 1
    for candidate in ordered:
        key = (candidate.file, candidate.chunk_id, candidate.line)
        if key in seen:
            continue
        if per_file.get(candidate.file, 0) >= 2 and len({c.file for c in selected}) < 4:
            continue
        selected.append(candidate)
        seen.add(key)
        per_file[candidate.file] = per_file.get(candidate.file, 0) + 1
        if len(selected) >= top_k:
            break

    return sorted(selected[:top_k], key=_candidate_rank, reverse=True)


def assess_coverage(
    understanding: QueryUnderstanding, candidates: Sequence[EvidenceCandidate]
) -> EvidenceCoverage:
    diagnostics: list[str] = []
    missing: list[str] = []
    covered: list[str] = []
    channels = {candidate.channel for candidate in candidates}
    roles = {candidate.role for candidate in candidates}
    files = {candidate.file for candidate in candidates if candidate.file}

    if candidates:
        covered.append("source")
    else:
        missing.append("source")
    if understanding.shape == "global":
        if "overview" in roles or any(file.lower().endswith((".md", ".mdx")) for file in files):
            covered.append("overview")
        else:
            missing.append("overview")
        if "implementation" in roles:
            covered.append("implementation")
        else:
            missing.append("implementation")
    if understanding.shape == "relational":
        if "graph" in channels or "relationship" in roles:
            covered.append("relationship")
        else:
            missing.append("relationship")
    if understanding.anchors:
        if "grep" in channels or "repo_map" in channels or any(
            anchor.lower() in f.lower() or anchor.lower() in c.excerpt.lower()
            for anchor in understanding.anchors
            for f in files
            for c in candidates
        ):
            covered.append("definition")
        else:
            missing.append("definition")
    if "source" not in missing and understanding.shape in {"local", "procedural"}:
        diagnostics.append("local/procedural query has source evidence")
    return EvidenceCoverage(
        sufficient=not missing,
        missing_facets=_ordered_unique(missing),
        covered_facets=_ordered_unique(covered),
        diagnostics=diagnostics,
    )


async def repair_missing_facets(
    *,
    ctx: RetrievalContext,
    product_id: str,
    understanding: QueryUnderstanding,
    existing: Sequence[EvidenceCandidate],
    missing: Sequence[str],
    top_k: int,
) -> tuple[list[EvidenceCandidate], list[RetrievalTrace]]:
    if not hasattr(ctx, "indexer"):
        return [], [
            RetrievalTrace(
                channel="grep",
                query=understanding.query,
                hits=0,
                detail="coverage repair skipped; indexer unavailable",
            )
        ]
    query_parts = [understanding.query, *understanding.anchors, *missing]
    query = " ".join(_ordered_unique(part for part in query_parts if part))
    found, trace, _ = await grep_candidates(
        ctx=ctx,
        product_id=product_id,
        query=query,
        limit=max(4, min(top_k, 8)),
    )
    seen = {(candidate.file, candidate.chunk_id, candidate.line) for candidate in existing}
    additions = [
        candidate
        for candidate in found
        if (candidate.file, candidate.chunk_id, candidate.line) not in seen
    ]
    for item in trace:
        item.detail = f"coverage repair for {', '.join(missing)}"
    return additions, trace


def _candidate_from_hit(hit: Hit, *, channel: EvidenceChannel) -> EvidenceCandidate:
    payload = hit.payload or {}
    file = str(payload.get("resource_uri") or "")
    content = str(payload.get("content") or "")
    context_path = payload.get("context_path")
    return EvidenceCandidate(
        chunk_id=hit.id,
        channel=channel,
        role=_role_for(
            file=file,
            context_path=str(context_path) if context_path else None,
            content=content,
            source=channel,
        ),
        score=hit.score,
        file=file,
        line=int(payload.get("start_line") or 0),
        end_line=int(payload["end_line"]) if payload.get("end_line") is not None else None,
        context_path=str(context_path) if context_path else None,
        # Return the full chunk body (chunks are capped at MAX_CHUNK_CHARS=1200 at
        # ingest). The old 900 cap truncated ~25% of every chunk, dropping the
        # behavioural detail that answers semantic questions (hurt context_recall).
        excerpt=_truncate(content, 1600),
        graph_node_ids=list(payload.get("graph_node_ids") or []),
        metadata={
            "source": hit.source,
            "kind": payload.get("kind"),
            "artifact_type": payload.get("artifact_type"),
        },
    )


def _candidate_from_symbol(score: float, symbol: Symbol) -> EvidenceCandidate:
    return EvidenceCandidate(
        chunk_id=f"repo_map:{symbol.file}:{symbol.line}:{symbol.name}",
        channel="repo_map",
        role="definition",
        score=score,
        file=symbol.file,
        line=symbol.line,
        excerpt=symbol.signature,
        metadata={"symbol": symbol.name, "kind": symbol.kind},
    )


def _score_symbols(symbols: list[Symbol], terms: Sequence[str]) -> list[tuple[float, Symbol]]:
    term_set = {term.lower() for term in terms if len(term) >= 3}
    scored: list[tuple[float, Symbol]] = []
    for symbol in symbols:
        haystack = f"{symbol.file} {symbol.name} {symbol.signature}".lower()
        overlap = sum(1 for term in term_set if term in haystack)
        score = overlap * 5.0 + (2.0 if symbol.kind in {"class", "struct", "interface"} else 1.0)
        if overlap:
            scored.append((score, symbol))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def _role_for(*, file: str, context_path: str | None, content: str, source: str) -> EvidenceRole:
    lower = f"{file} {context_path or ''} {content}".lower()
    if source == "summary":
        return "overview"
    if source == "graph":
        return "relationship"
    if file.lower().endswith((".md", ".mdx")):
        return "overview"
    if "test" in lower or "pytest" in lower or "assert " in lower:
        return "validation"
    if "class " in lower or "def " in lower or "function " in lower or "const " in lower:
        return "implementation"
    return "definition"


def _edge_types_for(understanding: QueryUnderstanding) -> list[str]:
    if understanding.shape == "relational":
        return ["IMPORTS", "CALLS", "DEPENDS_ON", "HANDLES", "READS", "WRITES", "DECLARES", "CONTAINS"]
    if understanding.shape == "global":
        return ["CONTAINS", "DECLARES", "DOCUMENTS", "COVERS", "IMPLEMENTS", "RELATED_TO"]
    return ["CONTAINS", "DECLARES", "HANDLES", "READS", "WRITES", "DOCUMENTS"]


def _grep_query(understanding: QueryUnderstanding) -> str:
    return " ".join(
        _ordered_unique(
            [
                understanding.query,
                *understanding.paths,
                *understanding.symbols,
                *understanding.routes,
                *understanding.config_keys,
            ]
        )
    )


def _drift_followup_queries(query: str, seeds: Sequence[EvidenceCandidate]) -> list[str]:
    fragments: list[str] = []
    for candidate in seeds:
        if candidate.channel not in {"summary", "repo_map"}:
            continue
        terms = [
            candidate.context_path or "",
            str(candidate.metadata.get("symbol") or ""),
            candidate.file,
        ]
        fragment = " ".join(term for term in terms if term).strip()
        if fragment:
            fragments.append(fragment)
    if not fragments:
        fragments = ["implementation", "data flow", "tests", "configuration"]
    return [
        f"{query} {fragment}".strip()
        for fragment in _ordered_unique(fragments)
    ][:4]


def _graph_rank_by_id(
    *,
    seed_ids: Sequence[str],
    nodes: Sequence[object],
    edges: Sequence[object] | None = None,
) -> dict[str, float]:
    ranks = {seed_id: 10.0 for seed_id in seed_ids}
    for node in nodes:
        stable_id = getattr(node, "stable_id", "")
        if not stable_id:
            continue
        confidence = float(getattr(node, "confidence", 1.0) or 1.0)
        labels = set(getattr(node, "labels", []) or [])
        label_boost = 1.5 if labels & {"APIEndpoint", "Function", "Class", "Document"} else 1.0
        ranks[stable_id] = max(ranks.get(stable_id, 0.0), 5.0 * confidence + label_boost)
    for edge in edges or []:
        confidence = float(getattr(edge, "confidence", 1.0) or 1.0)
        freshness = float(getattr(edge, "freshness", 1.0) or 1.0)
        method = getattr(edge, "extraction_method", "deterministic")
        method_boost = 1.0 if method == "deterministic" else 0.75
        edge_weight = _edge_type_weight(str(getattr(edge, "type", "")))
        score = 3.0 * confidence * freshness * method_boost * edge_weight
        for stable_id in (getattr(edge, "from_id", ""), getattr(edge, "to_id", "")):
            if stable_id:
                ranks[stable_id] = max(ranks.get(stable_id, 0.0), score)
    return ranks


# Graph/channel ranking weights below are hand-set priors, not constants of
# nature. They are calibrated against the `relational`/`graph` slice of
# tests/eval/queries.json via `tests.eval.harness.run_ablation`: a weight change
# is only justified when it moves recall@k/MRR/nDCG on that slice without
# regressing it. Don't tune these blind.
def _edge_type_weight(edge_type: str) -> float:
    return {
        "DECLARES": 1.4,
        "CONTAINS": 1.2,
        "HANDLES": 1.3,
        "EXPOSES": 1.3,
        "CALLS": 1.2,
        "COVERS": 1.1,
        "DOCUMENTS": 1.1,
        "MENTIONS": 0.6,
        "RELATED_TO": 0.5,
    }.get(edge_type, 1.0)


def _graph_path_label(paths: Sequence[dict[str, Any]], graph_node_ids: Sequence[str]) -> str:
    for path in paths:
        node_ids = [str(node_id) for node_id in path.get("node_ids", [])]
        edge_ids = [str(edge_id) for edge_id in path.get("edge_ids", [])]
        if node_ids and any(node_id in graph_node_ids for node_id in node_ids):
            return " -> ".join([*node_ids[:4], *edge_ids[:2]])
    return " -> ".join(graph_node_ids[:6])


def _graph_hit_rank(item: dict, rank_by_id: dict[str, float]) -> float:
    payload = item.get("payload") or {}
    ids = list(payload.get("graph_node_ids") or [])
    graph_score = max((rank_by_id.get(gid, 0.0) for gid in ids), default=0.0)
    artifact = payload.get("artifact_type")
    artifact_boost = 1.0 if artifact == "graph_community_summary" else 0.0
    return float(item.get("score") or 0.0) + graph_score + artifact_boost


def _take_channel(
    candidates: Sequence[EvidenceCandidate], channel: EvidenceChannel, count: int
) -> list[EvidenceCandidate]:
    if count <= 0:
        return []
    return [candidate for candidate in candidates if candidate.channel == channel][:count]


def _take_role(
    candidates: Sequence[EvidenceCandidate], role: EvidenceRole, count: int
) -> list[EvidenceCandidate]:
    if count <= 0:
        return []
    return [candidate for candidate in candidates if candidate.role == role][:count]


def _candidate_rank(candidate: EvidenceCandidate) -> float:
    # Exact filename↔anchor match is a strong relevance signal; a 0.5 bump (vs the
    # 0-1 reranker score) lifts the canonical class above same-prefix impl chunks
    # without overriding a clearly-more-relevant result.
    anchor_bonus = 0.5 if candidate.metadata.get("filename_anchor_match") else 0.0
    return candidate.score + anchor_bonus + _channel_weight(candidate.channel) + _role_weight(candidate.role)


def _basename_stem(uri: str) -> str:
    """Lowercased filename without directory or extension (``a/b/Foo.java`` -> ``foo``)."""
    base = (uri or "").split(":", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0].lower() if "." in base else base.lower()


# Channel/role weights are **tie-breakers** layered on the reranker's 0-1
# relevance score in ``_candidate_rank`` — the reranker decides relevance, these
# only break near-ties and keep a mild exact-match/structural preference. They
# were once 2-8, which dwarfed the 0-1 score and let thin grep line-snippets bury
# substantive reranked chunks (P3); scaled into a <0.2 band so score dominates.
# Channel diversity is guaranteed by the ``_take_channel`` quotas, not by these.
def _channel_weight(channel: EvidenceChannel) -> float:
    return {
        "grep": 0.16,
        "repo_map": 0.10,
        "graph": 0.08,
        "summary": 0.08,
        "hybrid": 0.06,
        "skill": 0.04,
    }[channel]


def _role_weight(role: EvidenceRole) -> float:
    return {
        "overview": 0.040,
        "definition": 0.040,
        "implementation": 0.036,
        "relationship": 0.030,
        "validation": 0.024,
        "skill_guidance": 0.020,
    }[role]


def _first_paragraph(body: str) -> str:
    for block in body.split("\n\n"):
        stripped = block.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return body.strip()


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ordered_unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


__all__ = [
    "EvidenceCandidate",
    "EvidenceCoverage",
    "EvidenceMode",
    "EvidenceSet",
    "QueryPlan",
    "QueryUnderstanding",
    "RetrievalTrace",
    "retrieve_evidence",
    "understand_query",
]
