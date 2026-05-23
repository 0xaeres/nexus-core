"""MCP tool implementations.

Each tool is an async function `(state: ToolState, **kwargs) -> dict`. The
server module wraps them and JSON-serialises the return for the TextContent
response.

Tools fall into two layers (§8):
  Guidance — find_skills, get_skill, report_outcome
  Context — query_code_context, hybrid_search_corpus
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from nexus.config import NexusConfig
from nexus.retrieval.pipeline import RetrievalContext, retrieve
from nexus.skills.models import OrgSkill, Skill
from nexus.skills.store import SkillStore

log = logging.getLogger(__name__)


@dataclass
class ToolState:
    """Shared, lazily-initialised handles for the lifetime of the MCP server."""

    product: str
    config: NexusConfig
    _ctx: RetrievalContext | None = None
    _store: SkillStore | None = None
    _outcomes: list[dict] = field(default_factory=list)

    @property
    def ctx(self) -> RetrievalContext:
        if self._ctx is None:
            self._ctx = RetrievalContext.from_config(self.config)
        return self._ctx

    @property
    def store(self) -> SkillStore:
        if self._store is None:
            # Resolve relative to current working dir per nexus.yaml hierarchy_root.
            root = Path(self.config.hierarchy_root)
            if not root.is_absolute():
                root = Path.cwd() / root
            self._store = SkillStore(root)
        return self._store


# ---------------------------------------------------------------- guidance tools


async def find_skills(
    state: ToolState,
    *,
    query: str,
    context: str = "general",
    current_file: str | None = None,
    top_k: int = 5,
) -> dict:
    """Return ranked skill summaries for a query+context.

    Selective serving keeps the context window tight:
      1. `current_file` filters by `applies_to.files` glob match.
      2. `context` (when not "general") filters by exact `applies_to.contexts` tag.
      3. Top-K survivors are ranked by lexical overlap + confidence.
      4. `composes_with` prerequisites are pulled in transitively.

    Skills with empty `applies_to.files` / `applies_to.contexts` are treated as
    universally applicable (no constraint), so they survive any filter.
    """
    all_skills = state.store.iter_skills()
    if not all_skills:
        return {"skills": [], "warning": "no skills found at hierarchy_root"}

    candidates = [
        s
        for s in all_skills
        if _matches_file_globs(current_file, s.applies_to.files)
        and _matches_context(context, s.applies_to.contexts)
    ]

    ql = (query + " " + context).lower()
    q_tokens = {t for t in _tokens(ql) if len(t) >= 3}
    scored: list[tuple[float, Skill | OrgSkill]] = []
    for s in candidates:
        haystack = f"{s.name} {' '.join(s.applies_to.contexts or [])} {s.body}".lower()
        h_tokens = set(_tokens(haystack))
        if not q_tokens:
            score = s.confidence
        else:
            overlap = len(q_tokens & h_tokens)
            score = overlap / max(len(q_tokens), 1) + 0.2 * s.confidence
        scored.append((score, s))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [s for _, s in scored[:top_k]]
    expanded = _expand_composes_with(top, all_skills)
    direct_ids = {s.id for s in top}
    score_by_id = {s.id: sc for sc, s in scored}

    out: list[dict] = []
    for s in expanded:
        out.append(
            {
                "id": s.id,
                "name": s.name,
                "kind": str(s.kind),
                "scope": str(_scope_of(s)),
                "confidence": s.confidence,
                "rank_score": round(score_by_id.get(s.id, 0.0), 4),
                "included_as": "match" if s.id in direct_ids else "prerequisite",
                "summary": _first_paragraph(s.body),
            }
        )
    return {
        "query": query,
        "context": context,
        "current_file": current_file,
        "filtered_from": len(all_skills),
        "skills": out,
    }


async def get_skill(state: ToolState, *, name: str) -> dict:
    """Return the full skill body + frontmatter."""
    skills = state.store.iter_skills()
    for s in skills:
        if s.name == name:
            return _serialise_full(s)
    return {"error": f"skill not found: {name}"}


async def report_outcome(
    state: ToolState,
    *,
    skill_name: str,
    succeeded: bool,
    notes: str = "",
) -> dict:
    """Append the outcome to an in-memory log. Persistence lands in Slice 5."""
    record = {
        "skill_name": skill_name,
        "succeeded": succeeded,
        "notes": notes,
        "ts": time.time(),
    }
    state._outcomes.append(record)
    log.info("outcome reported: %s", record)
    return {"ok": True, "received": record}


# ---------------------------------------------------------------- context tools


async def query_code_context(
    state: ToolState,
    *,
    symbol: str,
    file_glob: str = "**/*",
) -> dict:
    """Cheap symbol lookup — runs the retrieval pipeline in code-only mode."""
    result = await retrieve(
        ctx=state.ctx,
        product_id=state.product,
        query=symbol,
        top_k=10,
        mode="code",
    )
    return _render_retrieval(result)


async def hybrid_search_corpus(
    state: ToolState,
    *,
    query: str,
    product_id: str | None = None,
    top_k: int = 5,
) -> dict:
    """Hybrid retrieval (dense + BM25 + rerank) against the indexed corpus."""
    pid = product_id or state.product
    result = await retrieve(
        ctx=state.ctx, product_id=pid, query=query, top_k=top_k, mode="auto"
    )
    return _render_retrieval(result)


# ---------------------------------------------------------------- resource helpers


async def skill_hierarchy(state: ToolState) -> dict:
    skills = state.store.iter_skills()
    return {
        "product": state.product,
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "kind": str(s.kind),
                "scope": str(_scope_of(s)),
                "confidence": s.confidence,
            }
            for s in skills
        ],
    }


async def skill_markdown(state: ToolState, *, name: str) -> str:
    for s in state.store.iter_skills():
        if s.name == name:
            return s.body
    raise ValueError(f"skill not found: {name}")


async def corpus_summary(state: ToolState, *, product_id: str) -> dict:
    indexer = state.ctx.indexer
    try:
        code_count = await indexer.count(product_id=product_id, vector_kind="code")
        text_count = await indexer.count(product_id=product_id, vector_kind="text")
    except Exception as e:
        log.warning("corpus count failed: %s", e)
        return {"product_id": product_id, "error": str(e)}
    return {
        "product_id": product_id,
        "chunk_count": code_count + text_count,
        "code_chunk_count": code_count,
        "doc_chunk_count": text_count,
        "source_count": 0,  # populated when the source registry lands
    }


# ---------------------------------------------------------------- helpers


def _matches_file_globs(file_path: str | None, globs: list[str]) -> bool:
    """True if the file is in-scope for the skill's `applies_to.files`.

    Empty `globs` means "no constraint" — the skill applies to every file.
    When `file_path` is None, no current-file filter is requested and every skill
    passes.
    """
    if not globs:
        return True
    if file_path is None:
        return True
    p = PurePath(file_path)
    return any(p.full_match(g) for g in globs)


def _matches_context(requested: str, skill_contexts: list[str]) -> bool:
    """True if the requested context tag is allowed by the skill.

    Empty `skill_contexts` → universal (no context filter).
    Requested "general" is treated as no filter (matches everything).
    """
    if not skill_contexts:
        return True
    if not requested or requested == "general":
        return True
    return requested in skill_contexts


def _expand_composes_with(
    selected: Iterable[Skill | OrgSkill],
    all_skills: Iterable[Skill | OrgSkill],
) -> list[Skill | OrgSkill]:
    """Transitive closure under `composes_with`. Cycles are tolerated."""
    by_id: dict[str, Skill | OrgSkill] = {s.id: s for s in all_skills}
    result: dict[str, Skill | OrgSkill] = {}
    queue: list[Skill | OrgSkill] = list(selected)
    while queue:
        s = queue.pop(0)
        if s.id in result:
            continue
        result[s.id] = s
        for dep_id in s.composes_with:
            dep = by_id.get(dep_id)
            if dep is not None and dep.id not in result:
                queue.append(dep)
    return list(result.values())


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    for ch in text:
        if ch.isalnum() or ch == "_":
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur = []
    if cur:
        out.append("".join(cur))
    return out


def _first_paragraph(body: str) -> str:
    for block in body.split("\n\n"):
        stripped = block.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:300]
    return ""


def _scope_of(s: Skill | OrgSkill) -> str:
    return s.scope.value


def _serialise_full(s: Skill | OrgSkill) -> dict:
    out = s.model_dump(mode="json")
    out["id"] = s.id
    return out


def _render_retrieval(result) -> dict:
    return {
        "reranked": result.reranked,
        "hits": [
            {
                "score": h.score,
                "source": h.source,
                "anchor": f'{(h.payload or {}).get("resource_uri","?")}:'
                          f'{(h.payload or {}).get("start_line","?")}',
                "context_path": (h.payload or {}).get("context_path"),
                "content": (h.payload or {}).get("content"),
            }
            for h in result.hits
        ],
    }
