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

from nexus.retrieval.chunk_grep import grep_indexed_chunks
from nexus.retrieval.hybrid import Hit
from nexus.retrieval.pipeline import RetrievalContext, retrieve
from nexus.retrieval.repomap import Symbol, load_repo_map_for_product, topic_bias_terms
from nexus.skills.models import Skill

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
    coverage: EvidenceCoverage | None = None
    fallbacks: list[str] = Field(default_factory=list)
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
) -> EvidenceSet:
    """Retrieve a coverage-oriented evidence set for product/code questions."""
    started = time.perf_counter()
    understanding = understand_query(query, current_file=current_file)
    plan = _query_plan(understanding, query_mode=query_mode)
    trace: list[RetrievalTrace] = []

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
    if plan.mode == "drift_lite":
        drifted, drift_trace, drift_reranked = await drift_lite_candidates(
            ctx=ctx,
            product_id=product_id,
            query=query,
            seeds=pooled,
            top_k=max(top_k, 8),
        )
        pooled.extend(drifted)
        trace.extend(drift_trace)
        reranked = reranked or drift_reranked
        plan.channels_run = _ordered_unique([*plan.channels_run, "drift_lite"])
    pooled, mixed_reranked = await rerank_mixed_candidates(ctx=ctx, query=query, candidates=pooled)
    reranked = reranked or mixed_reranked
    if mixed_reranked:
        trace.append(
            RetrievalTrace(channel="mixed_rerank", query=query, hits=len(pooled))
        )
    merged = merge_candidates(pooled, understanding=understanding, top_k=top_k)
    coverage = assess_coverage(understanding, merged)
    if not coverage.sufficient:
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
            merged = merge_candidates([*merged, *repaired], understanding=understanding, top_k=top_k)
            coverage = assess_coverage(understanding, merged)
    plan.coverage = coverage
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
    elif mode == "local":
        seed_strategy = "explicit_anchor_graph_traversal" if understanding.anchors else "hybrid_hit_graph_seed"
    else:
        seed_strategy = "structural_summary_repo_map"
    return QueryPlan(
        mode=mode,
        shape=understanding.shape,
        anchors=understanding.anchors,
        graph_seed_strategy=seed_strategy,
    )


def understand_query(query: str, *, current_file: str | None = None) -> QueryUnderstanding:
    lower = query.lower()
    tokens = set(_SYMBOL_RE.findall(lower))
    paths = _ordered_unique([*( _PATH_RE.findall(query)), *([current_file] if current_file else [])])
    routes = _ordered_unique(_ROUTE_RE.findall(query))
    config_keys = _ordered_unique(_CONFIG_RE.findall(query))
    symbols = _ordered_unique(
        token
        for token in _SYMBOL_RE.findall(query)
        if ("_" in token or token[:1].isupper()) and token not in config_keys
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

    traversal = await graph_store.traverse(
        product_id=product_id,
        seed_ids=seed_ids[:8],
        edge_types=_edge_types_for(understanding),
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
    raw.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    out = [
        _candidate_from_hit(
            Hit(id=str(item["id"]), score=float(item.get("score") or 1.0), payload=item.get("payload") or {}, source="graph"),
            channel="graph",
        )
        for item in raw[:limit]
    ]
    return out, [RetrievalTrace(channel="graph", query=" ".join(understanding.anchors), hits=len(out))], False


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
        if (hit.payload or {}).get("artifact_type") == "summary"
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
) -> tuple[list[EvidenceCandidate], list[RetrievalTrace], bool]:
    """Deterministic broad-to-specific follow-up retrieval.

    This is deliberately not HyDE: no synthetic answer is generated. Follow-up
    queries come only from structural summaries, repo-map symbols, and the
    original user query.
    """
    followups = _drift_followup_queries(query, seeds)
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

    ordered = sorted(deduped.values(), key=_candidate_rank, reverse=True)
    selected: list[EvidenceCandidate] = []
    selected.extend(_take_channel(ordered, "grep", 2 if understanding.anchors else 1))
    selected.extend(_take_channel(ordered, "repo_map", 2 if understanding.shape in {"global", "local"} else 1))
    selected.extend(_take_channel(ordered, "graph", 3 if understanding.shape == "relational" else 1))
    selected.extend(_take_channel(ordered, "summary", 2 if understanding.shape == "global" else 1))
    selected.extend(_take_role(ordered, "overview", 2 if understanding.shape == "global" else 1))
    selected.extend(_take_channel(ordered, "skill", 1 if understanding.shape in {"procedural", "global"} else 0))

    seen = {(c.file, c.chunk_id, c.line) for c in selected}
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
        excerpt=_truncate(content, 900),
        graph_node_ids=list(payload.get("graph_node_ids") or []),
        metadata={"source": hit.source, "kind": payload.get("kind")},
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
    return candidate.score + _channel_weight(candidate.channel) + _role_weight(candidate.role)


def _channel_weight(channel: EvidenceChannel) -> float:
    return {
        "grep": 8.0,
        "repo_map": 5.0,
        "graph": 4.0,
        "summary": 4.0,
        "hybrid": 3.0,
        "skill": 2.0,
    }[channel]


def _role_weight(role: EvidenceRole) -> float:
    return {
        "overview": 2.0,
        "definition": 2.0,
        "implementation": 1.8,
        "relationship": 1.5,
        "validation": 1.2,
        "skill_guidance": 1.0,
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
