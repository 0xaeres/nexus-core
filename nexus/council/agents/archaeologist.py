"""Archaeologist — mines code patterns from the indexed corpus.

Workflow: retrieval (code mode, top-30) → ask the LLM to extract distinct
patterns, each tied to specific evidence chunks. Pure retrieve-then-prompt:
no agentic tool loop in Slice 3.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from nexus.config import NexusConfig
from nexus.council.agents._common import (
    evidence_for_prompt,
    hits_to_evidence,
    retrieval_unavailable,
)
from nexus.council.state import (
    AgentCost,
    CodePattern,
    CodePatterns,
    CouncilState,
    DeliberationMessage,
    EvidenceChunk,
)
from nexus.llm.client import ChatClient
from nexus.retrieval.pipeline import RetrievalContext, retrieve

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are the Archaeologist, an agent of the Nexus LLM Council. Your job is "
    "to mine code patterns from a software corpus and ground every pattern in "
    "specific file:line evidence. Never speculate about code you have not been "
    "shown."
)


_USER_TEMPLATE = """Topic: {topic}
Skill kind: {skill_kind}
Product: {product_id}

Below is the retrieved code evidence. Each excerpt is labelled [E1], [E2], etc.

{evidence}

Identify 3-7 distinct code patterns relevant to the topic. For each pattern,
state a short name, a one-paragraph description, and the evidence labels that
support it. Output ONLY JSON in this schema:

{{
  "patterns": [
    {{
      "name": "string",
      "description": "string",
      "evidence_labels": ["E1", "E3"]
    }}
  ],
  "notes": "optional caveats, max 2 sentences"
}}
"""


async def run(
    state: CouncilState,
    *,
    config: NexusConfig,
    retrieval: RetrievalContext,
    chat: ChatClient,
) -> dict:
    """Return a partial state update for LangGraph to merge."""
    topic = state["topic"]
    product_id = state["product_id"]

    result = await retrieve(
        ctx=retrieval,
        product_id=product_id,
        query=topic,
        top_k=30,
        mode="code",
        context_hint="archaeologist",
    )
    evidence_chunks = hits_to_evidence(result.hits, limit=20)
    unavail = retrieval_unavailable(result)
    if unavail:
        log.warning("archaeologist: %s", unavail)
        return _empty_update(unavail, evidence_chunks, chat.model)

    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _USER_TEMPLATE.format(
                topic=topic,
                skill_kind=state["skill_kind"],
                product_id=product_id,
                evidence=evidence_for_prompt(evidence_chunks),
            ),
        },
    ]
    payload, usage = await chat.chat_json(messages)
    patterns = _build_patterns(payload, evidence_chunks)

    return {
        "code_patterns": patterns,
        "deliberation": [
            DeliberationMessage(
                agent="archaeologist",
                timestamp=datetime.now(UTC).isoformat(),
                body=_render_summary(patterns),
                cite_ids=[e.chunk_id for e in evidence_chunks[:8]],
            )
        ],
        "costs": [
            AgentCost(
                agent="archaeologist",
                prompt_tokens=usage.prompt,
                completion_tokens=usage.completion,
                model=chat.model,
            )
        ],
    }


# ---------------------------------------------------------------- helpers


def _build_patterns(payload: dict, chunks: list[EvidenceChunk]) -> CodePatterns:
    by_label = {f"E{i+1}": c for i, c in enumerate(chunks)}
    out_patterns: list[CodePattern] = []
    for raw in payload.get("patterns", []):
        labels = raw.get("evidence_labels") or []
        evidence = [by_label[label] for label in labels if label in by_label]
        out_patterns.append(
            CodePattern(
                name=str(raw.get("name", "")).strip()[:80] or "unnamed",
                description=str(raw.get("description", "")).strip(),
                evidence=evidence,
            )
        )
    return CodePatterns(patterns=out_patterns, notes=str(payload.get("notes", "")))


def _render_summary(patterns: CodePatterns) -> str:
    if not patterns.patterns:
        return "No code patterns identified."
    lines = [f"Found {len(patterns.patterns)} patterns:"]
    for p in patterns.patterns:
        lines.append(f"  - **{p.name}** ({len(p.evidence)} citations)")
    if patterns.notes:
        lines.append(f"\nNotes: {patterns.notes}")
    return "\n".join(lines)


def _empty_update(reason: str, evidence: list[EvidenceChunk], model: str) -> dict:
    return {
        "code_patterns": CodePatterns(patterns=[], notes=reason),
        "deliberation": [
            DeliberationMessage(
                agent="archaeologist",
                timestamp=datetime.now(UTC).isoformat(),
                body=f"Could not produce patterns: {reason}",
                cite_ids=[e.chunk_id for e in evidence[:4]],
            )
        ],
        "costs": [AgentCost(agent="archaeologist", model=model)],
    }


# silence unused-import if json is removed from helpers above
_ = json
