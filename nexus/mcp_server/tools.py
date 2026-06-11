"""MCP tool implementations.

Each tool is an async function `(state: ToolState, **kwargs) -> dict`. The
server module wraps them and JSON-serialises the return for the TextContent
response.

Two layers:
  Guidance — find_skills, get_skill, report_outcome
  Context — query_code_context, hybrid_search_corpus
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from pydantic import BaseModel

from nexus.config import NexusConfig
from nexus.council.queue import ProposalQueue
from nexus.council.skill_catalog import fixed_skill_name, product_slug
from nexus.retrieval.pipeline import RetrievalContext, retrieve
from nexus.skills.models import Skill
from nexus.skills.store import SkillStore

log = logging.getLogger(__name__)


class ToolError(BaseModel):
    error: str
    product_id: str | None = None


@dataclass
class ToolState:
    """Shared, lazily-initialised handles for the lifetime of the MCP server."""

    product: str
    config: NexusConfig
    _ctx: RetrievalContext | None = None
    _store: SkillStore | None = None
    _queue: ProposalQueue | None = None
    _outcomes: list[dict] = field(default_factory=list)

    @property
    def ctx(self) -> RetrievalContext:
        if self._ctx is None:
            self._ctx = RetrievalContext.from_config(self.config)
        return self._ctx

    @property
    def store(self) -> SkillStore:
        if self._store is None:
            root = Path(self.config.hierarchy_root)
            if not root.is_absolute():
                root = Path.cwd() / root
            self._store = SkillStore(root)
        return self._store

    @property
    def queue(self) -> ProposalQueue:
        if self._queue is None:
            self._queue = ProposalQueue(Path(self.config.storage.proposal_queue))
        return self._queue


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

    Selective serving:
      1. `current_file` filters by `applies_to.files` glob match.
      2. `context` (when not "general") filters by exact `applies_to.contexts` tag.
      3. Top-K survivors are ranked by lexical overlap + confidence.
    """
    all_skills = state.store.iter_skills()
    if not all_skills:
        return {"skills": [], "warning": "no skills found at hierarchy_root"}

    product_skills = [s for s in all_skills if s.product == state.product]
    master_skills = [s for s in product_skills if s.tier == "product_master"]
    canonical_master = fixed_skill_name(product_slug(state.product), "skill")
    candidates = [
        s
        for s in product_skills
        if _matches_file_globs(current_file, s.applies_to.files)
        and _matches_context(context, s.applies_to.contexts)
        and s.tier != "product_master"
    ]

    ql = (query + " " + context).lower()
    q_tokens = {t for t in _tokens(ql) if len(t) >= 3}
    scored: list[tuple[float, Skill]] = []
    for s in candidates:
        haystack = (
            f"{s.name} {s.description} {' '.join(s.applies_to.contexts or [])} {s.body}"
        ).lower()
        h_tokens = set(_tokens(haystack))
        if not q_tokens:
            score = s.confidence
        else:
            overlap = len(q_tokens & h_tokens)
            score = overlap / max(len(q_tokens), 1) + 0.2 * s.confidence
        scored.append((score, s))

    scored.sort(key=lambda x: x[0], reverse=True)
    masters = sorted(
        master_skills,
        key=lambda s: (0 if s.name == canonical_master else 1, -s.confidence, s.name),
    )[:1]
    remaining = max(top_k - len(masters), 0)
    top = [*masters, *[s for _, s in scored[:remaining] if s not in masters]]

    out: list[dict] = []
    for s in top:
        out.append(
            {
                "id": s.id,
                "name": s.name,
                "tier": s.tier,
                "confidence": s.confidence,
                "summary": s.description or _first_paragraph(s.body),
            }
        )
    return {
        "query": query,
        "context": context,
        "current_file": current_file,
        "filtered_from": len(product_skills),
        "skills": out,
    }


async def get_skill(state: ToolState, *, name: str) -> dict:
    """Return the full skill body + frontmatter."""
    for s in state.store.iter_skills():
        if s.product != state.product:
            continue
        if s.name == name:
            out = s.model_dump(mode="json")
            out["id"] = s.id
            return out
    return {"error": f"skill not found: {name}"}


async def report_outcome(
    state: ToolState,
    *,
    skill_name: str,
    succeeded: bool,
    notes: str = "",
) -> dict:
    """Persist an outcome signal for future skill improvement."""
    record = {
        "skill_name": skill_name,
        "succeeded": succeeded,
        "notes": notes,
        "ts": time.time(),
    }
    state._outcomes.append(record)
    signal_id = state.queue.record_skill_signal(
        product_id=state.product,
        source_type="mcp_outcome",
        skill_name=skill_name,
        text=notes or ("Skill succeeded." if succeeded else "Skill failed."),
        metadata={"succeeded": succeeded, "ts": record["ts"]},
    )
    record["signal_id"] = signal_id
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
    if product_id is not None and product_id != state.product:
        return ToolError(error="cross-product corpus search is not allowed").model_dump(
            exclude_none=True
        )
    pid = state.product
    result = await retrieve(
        ctx=state.ctx, product_id=pid, query=query, top_k=top_k, mode="auto"
    )
    return _render_retrieval(result)


# ---------------------------------------------------------------- resource helpers


async def skill_hierarchy(state: ToolState) -> dict:
    return {
        "product": state.product,
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "tier": s.tier,
                "confidence": s.confidence,
            }
            for s in state.store.iter_skills()
            if s.product == state.product
        ],
    }


async def skill_markdown(state: ToolState, *, name: str) -> str:
    for s in state.store.iter_skills():
        if s.product != state.product:
            continue
        if s.name == name:
            return s.body
    raise ValueError(f"skill not found: {name}")


async def corpus_summary(state: ToolState, *, product_id: str) -> dict:
    if product_id != state.product:
        return ToolError(
            product_id=product_id,
            error="cross-product corpus access is not allowed",
        ).model_dump(exclude_none=True)
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
        "source_count": 0,
    }


# ---------------------------------------------------------------- helpers


def _matches_file_globs(file_path: str | None, globs: list[str]) -> bool:
    """Match `applies_to.files` globs against a repo-relative path.

    Skill authors should prefer recursive patterns such as `**/*.py`; those
    preserve the same intent under Python 3.13 `PurePath.full_match()` and the
    older `PurePath.match()` fallback.
    """
    if not globs:
        return True
    if file_path is None:
        return True
    p = PurePath(file_path)
    # Keep this helper usable in older local envs even though CI targets 3.13+.
    full_match = getattr(p, "full_match", None)
    if full_match is not None:
        return any(full_match(g) for g in globs)
    return any(p.match(g) for g in globs)


def _matches_context(requested: str, skill_contexts: list[str]) -> bool:
    if not skill_contexts:
        return True
    if not requested or requested == "general":
        return True
    return requested in skill_contexts


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
