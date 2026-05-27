"""Drafter — retrieves evidence and writes the initial skill proposal.

Output is plain markdown (not JSON-wrapped) so we don't burn ~30-40% of the
token budget on JSON escaping. Truncation is handled by chat_markdown's
auto-continuation (aider/cursor pattern). Missing sections trigger a single
targeted section-fill call. Caller can rely on the returned proposal having
all required sections — see CompletenessReport in skill_parser.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from nexus.config import NexusConfig
from nexus.council.agents._common import (
    evidence_for_prompt,
    hits_to_evidence,
)
from nexus.council.errors import CouncilNoEvidence
from nexus.council.skill_parser import (
    parse_skill_markdown,
    strip_uncited_rules,
    validate_completeness,
)
from nexus.council.state import (
    AgentCost,
    CouncilState,
    DeliberationMessage,
    EvidenceChunk,
)
from nexus.llm.client import ChatClient, TokenUsage
from nexus.retrieval.pipeline import RetrievalContext, retrieve
from nexus.retrieval.repomap import load_repo_map_for_product, topic_bias_terms
from nexus.skills.models import SkillProposal, compute_confidence

log = logging.getLogger(__name__)


_SYSTEM = (
    "You are the Drafter, an agent of the Nexus LLM Council. Your job: read "
    "retrieved code and documentation evidence for a software product, and "
    "produce a SKILL — a short, opinionated Markdown playbook that guides "
    "future AI agents working in this codebase. Every non-trivial claim must "
    "carry a `[file: path:line]` citation drawn from the evidence below. "
    "Uncited claims in the Rules section will be stripped from your output."
)


_USER_TEMPLATE = """Topic: {topic}
Product: {product_id}

# Retrieved evidence

Each excerpt is labelled [E1], [E2], etc. with its file:line anchor.

{evidence}

# Task — output plain Markdown (no JSON wrapper, no code fences around the whole thing)

Required structure (in this order):

# {{kebab-case-name}}

A 2-3 sentence opening paragraph framing why this skill matters.

## Rules

1. First rule with `[file: path:line]` citation. Be concrete and actionable.
2. Second rule with citation.
3. Third rule with citation.
(3-7 numbered rules total; each MUST carry at least one citation)

## Anti-patterns

- Concrete thing to avoid, with `[file: path:line]` citation when supported by evidence.
- Another anti-pattern (uncited general-best-practice is allowed here).

Output the markdown directly. Do NOT wrap it in JSON. Do NOT add commentary
before or after. The first line must be the `# ` heading.
"""


_SECTION_FILL_TEMPLATE = """The following sections are missing or too short in
the draft you just produced:

{missing}

Here is the draft as it stands:

{current_body}

Here is the available evidence (you must cite by file:line):

{evidence}

Produce ONLY the missing section(s), in markdown, in the order listed above.
Do not repeat sections that already exist. Each section starts with its `##`
heading. Maintain the same style + voice as the existing draft.
"""


async def run(
    state: CouncilState,
    *,
    config: NexusConfig,
    retrieval: RetrievalContext,
    chat: ChatClient,
) -> dict:
    topic = state["topic"]
    product_id = state["product_id"]

    result = await retrieve(
        ctx=retrieval, product_id=product_id, query=topic, top_k=20, mode="auto"
    )
    evidence = hits_to_evidence(result.hits, limit=20)

    if not evidence:
        raise _no_evidence_error(result, config)

    repo_map = load_repo_map_for_product(config, product_id)
    repo_map_block = repo_map.render(bias_terms=topic_bias_terms(topic), token_budget=500)
    system_prompt = _SYSTEM if not repo_map_block else f"{_SYSTEM}\n\n{repo_map_block}"

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": _USER_TEMPLATE.format(
                topic=topic,
                product_id=product_id,
                evidence=evidence_for_prompt(evidence),
            ),
        },
    ]
    resp = await chat.chat_markdown(messages, max_tokens=3000, max_continuations=2)
    usage = resp.usage
    body = resp.content.strip()

    # ---- completeness gate: section-fill any missing required sections ----
    report = validate_completeness(body)
    fill_attempts = 0
    while not report.is_complete and fill_attempts < 1:
        fill_attempts += 1
        missing_summary = _format_missing(report)
        log.info("drafter: section-fill pass — %s", missing_summary)
        fill_messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": _SECTION_FILL_TEMPLATE.format(
                    missing=missing_summary,
                    current_body=body,
                    evidence=evidence_for_prompt(evidence),
                ),
            },
        ]
        fill_resp = await chat.chat_markdown(
            fill_messages, max_tokens=1500, max_continuations=1
        )
        body = _merge_section_fill(body, fill_resp.content.strip())
        usage = TokenUsage(
            prompt=usage.prompt + fill_resp.usage.prompt,
            completion=usage.completion + fill_resp.usage.completion,
        )
        report = validate_completeness(body)

    # ---- parse + post-hoc guardrails ----
    body, dropped = strip_uncited_rules(body)
    parsed = parse_skill_markdown(body, fallback_name=topic, evidence=evidence)

    paragraphs = max(1, parsed.body.count("\n\n") + 1)
    confidence = compute_confidence(
        citations=parsed.citations, paragraphs=paragraphs, revision_count=0
    )

    proposal = SkillProposal(
        id=str(uuid.uuid4()),
        name=parsed.name,
        body=parsed.body,
        citations=parsed.citations,
        confidence=confidence,
        status="pending",
        created_at=datetime.now(UTC).isoformat(),
    )

    note_parts: list[str] = []
    if dropped:
        note_parts.append(f"{dropped} uncited line(s) stripped")
    if resp.truncated:
        note_parts.append("continued after truncation")
    if fill_attempts:
        note_parts.append(f"{fill_attempts} section-fill pass(es)")
    note = f" ({'; '.join(note_parts)})" if note_parts else ""

    summary = (
        f"Drafted **{parsed.name}** — confidence {confidence:.2f}, "
        f"{len(parsed.citations)} citations, {paragraphs} paragraphs{note}."
    )

    return {
        "evidence": evidence,
        "proposal": proposal,
        "proposal_id": proposal.id,
        "revision_count": 0,
        "critique": None,
        "deliberation": [
            DeliberationMessage(
                agent="drafter",
                timestamp=datetime.now(UTC).isoformat(),
                body=summary,
                cite_ids=[c.id for c in parsed.citations if c.id],
            )
        ],
        "costs": [
            AgentCost(
                agent="drafter",
                prompt_tokens=usage.prompt,
                completion_tokens=usage.completion,
                model=chat.model,
            )
        ],
    }


# ---------------------------------------------------------------- helpers


def _format_missing(report) -> str:
    parts = list(report.missing_sections) + list(report.short_sections)
    return ", ".join(parts) if parts else "(none)"


def _merge_section_fill(current: str, fill: str) -> str:
    """Append section-fill output to the current body.

    The fill prompt is constrained to emit only the missing sections, each
    starting with `##`. We append a blank line then the fill content — no
    deduplication needed because the prompt forbids repeating existing sections.
    """
    fill = fill.strip()
    if not fill:
        return current
    return current.rstrip() + "\n\n" + fill + "\n"


def _no_evidence_error(result, config: NexusConfig) -> CouncilNoEvidence:
    gate = config.ingestion.quality_gate_threshold
    if result.seed_count and result.filtered_by_gate:
        best = result.best_score_before_gate
        best_text = "unknown" if best is None else f"{best:.3g}"
        detail = (
            "retrieval found "
            f"{result.seed_count} candidate chunk(s), but quality_gate_threshold={gate:g} "
            f"filtered every reranked hit (best_score={best_text}). Lower "
            "ingestion.quality_gate_threshold, or set it to 0.0 for local Jina "
            "reranker scores, then retry the council session."
        )
        return CouncilNoEvidence(
            user_message=(
                "Council stopped before drafting because the retrieval quality gate "
                "filtered every candidate evidence chunk. Lower the quality gate and "
                "run the council again."
            ),
            detail=detail,
        )
    return CouncilNoEvidence(
        user_message=(
            "Council stopped before drafting because no evidence chunks were found. "
            "Sync source content, then run the council again."
        ),
        detail="retrieval found no candidate chunks; sync source content before running council",
    )


# silence unused import in this module
_ = EvidenceChunk
