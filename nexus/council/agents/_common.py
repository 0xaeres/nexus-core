"""Shared council-agent helpers — evidence rendering, retrieval-empty checks."""

from __future__ import annotations

from nexus.council.state import EvidenceChunk
from nexus.retrieval.evidence import EvidenceSet
from nexus.retrieval.hybrid import Hit


def hits_to_evidence(hits: list[Hit], *, limit: int = 20) -> list[EvidenceChunk]:
    out: list[EvidenceChunk] = []
    for h in hits[:limit]:
        payload = h.payload or {}
        out.append(
            EvidenceChunk(
                chunk_id=h.id,
                file=str(payload.get("resource_uri", "?")),
                line=int(payload.get("start_line", 0) or 0),
                score=h.score,
                excerpt=_truncate(str(payload.get("content", "")), 600),
            )
        )
    return out


def evidence_set_to_evidence(result: EvidenceSet, *, limit: int = 20) -> list[EvidenceChunk]:
    out: list[EvidenceChunk] = []
    for candidate in result.candidates[:limit]:
        out.append(
            EvidenceChunk(
                chunk_id=candidate.chunk_id,
                file=candidate.file or "?",
                line=candidate.line,
                score=candidate.score,
                excerpt=_truncate(candidate.excerpt, 600),
            )
        )
    return out


def evidence_for_prompt(chunks: list[EvidenceChunk]) -> str:
    """Render evidence chunks as a single text block the LLM can cite by index."""
    if not chunks:
        return "(no retrieval results)"
    lines: list[str] = []
    for i, c in enumerate(chunks, start=1):
        anchor = f"{c.file}:{c.line}"
        lines.append(f"[E{i}] {anchor}  (score={c.score:.3f}, id={c.chunk_id})")
        lines.append(c.excerpt)
        lines.append("")
    return "\n".join(lines)


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else (s[: n - 1] + "…")
