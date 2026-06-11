"""Proposal approval flow: queue row -> Skill model -> SKILL.md -> git -> Qdrant.

One async function `approve_proposal` is the source of truth. The API and CLI
both call it. Idempotent within a session (re-approving an already-approved
proposal is a no-op).
"""

from __future__ import annotations

import logging
import re
import textwrap
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from nexus.config import NexusConfig
from nexus.council.queue import ProposalQueue
from nexus.ingest.embedder import EmbedderClient
from nexus.ingest.indexer import Indexer
from nexus.ingest.models import Chunk, ChunkKind, EmbeddedChunk, ResourceRef
from nexus.retrieval.sparse import aencode_passages
from nexus.skills.git import commit_and_push
from nexus.skills.models import AppliesTo, Citation, Provenance, Skill, SkillCoverage
from nexus.skills.store import SkillStore

log = logging.getLogger(__name__)
DOC_LINE_WIDTH = 100


class ApprovalError(RuntimeError):
    pass


class ApprovalPublishError(ApprovalError):
    pass


async def approve_proposal(
    *,
    proposal_id: str,
    actor: str,
    config: NexusConfig,
    queue: ProposalQueue,
) -> dict:
    row = queue.get(proposal_id)
    if not row:
        raise ApprovalError(f"proposal not found: {proposal_id}")
    if row["status"] == "approved":
        return {
            "ok": True,
            "skipped": "already_approved",
            "skill_id": f"{row.get('product_id')}/{row.get('name')}",
        }

    skill = _row_to_skill(row, actor=actor)
    store = SkillStore(_resolve_root(config.hierarchy_root))
    rel = SkillStore.relative_path_for(skill)
    target = store.root / rel
    previous = target.read_text(encoding="utf-8") if target.exists() else None
    path = store.save(skill)
    log.info("approval: wrote %s", path)

    publish = commit_and_push(
        store.root,
        message=f"skill: {skill.name} approved by {actor}",
        push=True,
    )
    if not publish.committed or not publish.pushed:
        if previous is None:
            path.unlink(missing_ok=True)
            if path.name == "SKILL.md":
                with suppress(OSError):
                    path.parent.rmdir()
        else:
            path.write_text(previous, encoding="utf-8")
        detail = f": {publish.error}" if publish.error else ""
        raise ApprovalPublishError(
            "skill file was written, but Git commit/push did not complete; "
            f"proposal remains pending{detail}"
        )

    chunks_indexed = await _embed_skill_body(
        skill, source_uri=str(path), config=config
    )
    index_status = "indexed" if chunks_indexed > 0 else "pending"

    relative_path = rel
    queue.record_publish_result(
        proposal_id,
        skill_path=relative_path,
        git_committed=publish.committed,
        skill_index_status=index_status,
        skill_index_error=(
            "" if chunks_indexed > 0 else "approved skill was not embedded; retry indexing"
        ),
    )
    queue.update_status(proposal_id, status="approved", actor=actor)
    queue.record_skill_signal(
        product_id=skill.product,
        source_type="approval",
        skill_name=skill.name,
        proposal_id=proposal_id,
        session_id=row.get("session_id"),
        text=f"Approved by {actor}.",
        metadata={"actor": actor, "quality_score": skill.quality_score},
    )
    return {
        "ok": True,
        "skill_id": skill.id,
        "path": relative_path,
        "git_committed": publish.committed,
        "chunks_indexed": chunks_indexed,
        "skill_index_status": index_status,
    }


# ---------------------------------------------------------------- helpers


def _row_to_skill(row: dict, *, actor: str) -> Skill:
    citations = [
        Citation(
            id=c.get("id"),
            file=c["file"],
            line=int(c["line"]),
            excerpt=c.get("excerpt", ""),
        )
        for c in row.get("citations", [])
    ]
    crit = row.get("adversary_critique")
    return Skill(
        name=row["name"],
        description=row.get("description", ""),
        product=row["product_id"],
        tier=row.get("tier") or "domain",
        parent=row.get("parent"),
        related=list(row.get("related") or []),
        coverage=SkillCoverage(**(row.get("coverage") or {})),
        version=1,
        confidence=float(row["confidence"]),
        eval_status=row.get("eval_status", "not_run"),
        eval_summary=row.get("eval_summary", ""),
        eval_failures=list(row.get("eval_failures") or []),
        quality_score=float(row.get("quality_score") or 0.0),
        signals_used=list(row.get("signals_used") or []),
        applies_to=AppliesTo(),
        provenance=Provenance(
            council_session=row.get("session_id"),
            validated_by=actor,
            validated_at=datetime.now(UTC).isoformat(),
            evidence_chunks=[c.id for c in citations if c.id],
            adversary_critique=(crit.get("recommendation") if crit else None),
            revision_count=1 if crit and crit.get("severity") == "blocking" else 0,
        ),
        body=_wrap_markdown_body(row["body"]),
    )


def _wrap_markdown_body(body: str, *, width: int = DOC_LINE_WIDTH) -> str:
    """Wrap prose Markdown lines while preserving structural and code blocks."""
    lines = body.strip().splitlines()
    out: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = " ".join(line.strip() for line in paragraph if line.strip())
        out.extend(textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False) or [""])
        paragraph.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush_paragraph()
            out.append(line.rstrip())
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(line.rstrip())
            continue
        if not stripped:
            flush_paragraph()
            if out and out[-1] != "":
                out.append("")
            continue
        if _preserve_markdown_line(stripped, line):
            flush_paragraph()
            out.extend(_wrap_structural_line(line.rstrip(), width=width))
            continue
        paragraph.append(line)

    flush_paragraph()
    return "\n".join(out).strip() + "\n"


def _preserve_markdown_line(stripped: str, original: str) -> bool:
    return (
        stripped.startswith(("#", ">", "|"))
        or stripped.startswith(("-", "*", "+"))
        or bool(re.match(r"^\d+\.\s+", stripped))
        or original.startswith(("    ", "\t"))
        or re.match(r"^\[[^\]]+\]:\s+", stripped) is not None
    )


def _wrap_structural_line(line: str, *, width: int) -> list[str]:
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    match = re.match(r"^((?:[-*+]|\d+\.)\s+)(.*)$", stripped)
    if match is None or len(line) <= width:
        return [line]
    prefix, text = match.groups()
    initial = f"{indent}{prefix}"
    subsequent = " " * len(initial)
    return textwrap.wrap(
        text,
        width=width,
        initial_indent=initial,
        subsequent_indent=subsequent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _resolve_root(root: Path) -> Path:
    p = Path(root)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


async def _embed_skill_body(
    skill: Skill, *, source_uri: str, config: NexusConfig
) -> int:
    """Embed the approved skill body as a doc chunk for retrievability."""
    if not skill.body.strip():
        return 0

    embedder = EmbedderClient(
        base_url=config.models.embedding.url or "http://localhost:8080"
    )
    indexer = Indexer(url=config.vector_store.url)
    try:
        resource = ResourceRef(
            source_id=f"skill:{skill.product}",
            uri=source_uri,
            mime="text/markdown",
        )
        chunk = Chunk(
            product_id=skill.product,
            resource=resource,
            content=skill.body,
            start_line=1,
            end_line=skill.body.count("\n") + 1,
            kind=ChunkKind.DOC,
            context_path=f"skill:{skill.name}",
        )
        try:
            await indexer.ensure_collections()
        except Exception as e:
            log.warning("approval: ensure_collections failed: %s", e)
            return 0

        try:
            embedded = await embedder.embed_chunks([chunk])
        except Exception as e:
            log.warning("approval: embedder unreachable, skipping vector upsert: %s", e)
            return 0

        sparse = await aencode_passages([chunk.content])
        sparse_by_id = {chunk.id: sparse[0]} if sparse else {}
        return await indexer.upsert(embedded, sparse_by_id=sparse_by_id)
    finally:
        await embedder.aclose()
        await indexer.aclose()


def _embedded(c: Chunk, vec: list[float]) -> EmbeddedChunk:
    return EmbeddedChunk(chunk=c, vector=vec, vector_name="dense_text")
