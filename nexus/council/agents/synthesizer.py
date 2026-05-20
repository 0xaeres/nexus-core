"""Synthesizer — aggregate CodePatterns + DomainContext into a SkillProposal.

The synthesizer's output is the actual skill the human reviewer sees. Every
non-trivial assertion must carry a `[file: path:line]` citation. We enforce
this by running a faithfulness pass that strips uncited claims (§19 guardrail).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from nexus.config import NexusConfig
from nexus.council.state import (
    AgentCost,
    CouncilState,
    DeliberationMessage,
    EvidenceChunk,
)
from nexus.llm.client import ChatClient
from nexus.skills.models import (
    Citation,
    SkillProposal,
    compute_confidence,
)

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are the Synthesizer, an agent of the Nexus LLM Council. You take "
    "evidence from the Archaeologist (code patterns) and the Domain Expert "
    "(documentation context) and produce a SKILL: a short, opinionated Markdown "
    "document that guides future AI agents working in this codebase. EVERY "
    "non-trivial claim must carry a citation in the form `[file: path:line]`. "
    "Uncited claims will be stripped from your output."
)


_USER_TEMPLATE = """Topic: {topic}
Skill kind: {skill_kind}
Product: {product_id}

# Inputs

## Code patterns (from the Archaeologist)
{patterns_block}

## Domain context (from the Domain Expert)
{domain_block}

# Available evidence (you must cite these by their file:line anchors only)
{evidence_table}
{critique_block}
# Task

Write the skill as Markdown. Structure:

1. A clear `# Title`.
2. A short opening paragraph (2-3 sentences) framing why this skill matters.
3. A `## Rules` section with 3-7 numbered rules. Each rule includes at least
   one `[file: path:line]` citation drawn from the evidence above.
4. A `## Anti-patterns` section with concrete things to avoid (cite where
   relevant; uncited entries allowed if they are general best-practice).

Output ONLY a JSON object in this schema (no markdown fences):

{{
  "name": "kebab-case skill name, e.g. swap-fee-math",
  "body": "the markdown body as a single string",
  "citations": [
    {{"file": "path", "line": 42, "excerpt": "..." }}
  ]
}}

`citations` must contain every distinct file:line you used in the body.
"""


async def run(
    state: CouncilState,
    *,
    config: NexusConfig,
    chat: ChatClient,
) -> dict:
    topic = state["topic"]
    code_patterns = state.get("code_patterns")
    domain_context = state.get("domain_context")
    evidence = _collect_evidence(state)

    if not evidence:
        proposal = _empty_proposal(topic)
        return _wrap_update(proposal, chat.model, prompt_tok=0, completion_tok=0)

    prior_critique = state.get("critique")
    prior_proposal = state.get("proposal")
    is_redraft = prior_critique is not None and prior_proposal is not None
    critique_block = _render_critique_block(prior_critique, prior_proposal) if is_redraft else ""

    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _USER_TEMPLATE.format(
                topic=topic,
                skill_kind=state["skill_kind"],
                product_id=state["product_id"],
                patterns_block=_render_patterns(code_patterns),
                domain_block=_render_domain(domain_context),
                evidence_table=_render_evidence_table(evidence),
                critique_block=critique_block,
            ),
        },
    ]
    payload, usage = await chat.chat_json(messages, max_tokens=3000)

    name = _normalise_name(payload.get("name") or topic)
    raw_body = str(payload.get("body", "")).strip()
    body, dropped = _strip_uncited_assertions(raw_body, evidence)
    citations = _build_citations(payload.get("citations") or [], evidence)
    paragraphs = max(1, body.count("\n\n") + 1)
    new_revision_count = 1 if is_redraft else 0
    confidence = compute_confidence(
        citations=citations, paragraphs=paragraphs, revision_count=new_revision_count
    )

    # Re-use the prior proposal id when redrafting so the queue row updates in place.
    proposal_id = prior_proposal.id if is_redraft else str(uuid.uuid4())

    proposal = SkillProposal(
        id=proposal_id,
        name=name,
        body=body,
        citations=citations,
        confidence=confidence,
        status="pending",
        created_at=datetime.now(UTC).isoformat(),
    )

    body_note = ""
    if dropped:
        body_note = f"\n\n_({dropped} uncited sentence(s) stripped before storage)_"
    verb = "Redrafted" if is_redraft else "Drafted"
    summary = (
        f"{verb} **{name}** (confidence {confidence:.2f}, "
        f"{len(citations)} citations, {paragraphs} paragraphs)."
        + body_note
    )
    return _wrap_update(
        proposal,
        chat.model,
        prompt_tok=usage.prompt,
        completion_tok=usage.completion,
        summary=summary,
        is_redraft=is_redraft,
    )


# ---------------------------------------------------------------- helpers


def _collect_evidence(state: CouncilState) -> list[EvidenceChunk]:
    out: list[EvidenceChunk] = []
    seen: set[str] = set()
    code = state.get("code_patterns")
    if code:
        for p in code.patterns:
            for e in p.evidence:
                if e.chunk_id not in seen:
                    out.append(e)
                    seen.add(e.chunk_id)
    domain = state.get("domain_context")
    if domain:
        for e in domain.evidence:
            if e.chunk_id not in seen:
                out.append(e)
                seen.add(e.chunk_id)
    return out


def _render_patterns(cp) -> str:
    if not cp or not cp.patterns:
        return "_(none)_"
    lines: list[str] = []
    for p in cp.patterns:
        lines.append(f"- **{p.name}**: {p.description}")
        anchors = ", ".join(f"{e.file}:{e.line}" for e in p.evidence)
        lines.append(f"  Evidence: {anchors}")
    return "\n".join(lines)


def _render_domain(dc) -> str:
    if not dc:
        return "_(none)_"
    parts: list[str] = []
    if dc.summary:
        parts.append(dc.summary)
    if dc.vocabulary:
        parts.append(f"Vocabulary: {', '.join(dc.vocabulary)}")
    if dc.entity_relationships:
        parts.append("Relationships:\n" + "\n".join(f"- {r}" for r in dc.entity_relationships))
    return "\n\n".join(parts) or "_(empty)_"


def _render_evidence_table(evidence: list[EvidenceChunk]) -> str:
    lines: list[str] = []
    for e in evidence:
        lines.append(f"- `[file: {e.file}:{e.line}]` {e.excerpt[:140]}")
    return "\n".join(lines)


_NAME_RE = re.compile(r"[^a-z0-9-]+")
_DASH_RUN = re.compile(r"-{2,}")


def _normalise_name(raw: str) -> str:
    s = raw.strip().lower().replace("_", "-").replace(" ", "-")
    s = _NAME_RE.sub("-", s)
    s = _DASH_RUN.sub("-", s).strip("-")
    return s[:60] or "untitled-skill"


_CITATION_RE = re.compile(r"\[(?:file|cve)[^\]]+\]", re.IGNORECASE)


def _strip_uncited_assertions(body: str, evidence: list[EvidenceChunk]) -> tuple[str, int]:
    """Drop sentences in numbered/bulleted rules that lack any citation marker.

    The guardrail in §19 says zero uncited *assertions*. We apply it conservatively:
    only inside `## Rules` numbered lists, drop list items with no `[file:` or
    `[CVE-...]` marker. Other prose passes through unchanged.
    """
    rules_block = re.search(r"##\s+Rules(.*?)(?=\n##\s+|\Z)", body, flags=re.DOTALL | re.IGNORECASE)
    if not rules_block:
        return body, 0

    block_text = rules_block.group(1)
    new_lines: list[str] = []
    dropped = 0
    for line in block_text.splitlines():
        is_list_item = bool(re.match(r"^\s*(?:\d+\.|[-*])\s", line))
        if is_list_item and not _CITATION_RE.search(line):
            dropped += 1
            continue
        new_lines.append(line)
    if dropped == 0:
        return body, 0
    new_block = "\n".join(new_lines)
    return body[: rules_block.start(1)] + new_block + body[rules_block.end(1) :], dropped


def _build_citations(raw: list, evidence: list[EvidenceChunk]) -> list[Citation]:
    by_anchor: dict[tuple[str, int], EvidenceChunk] = {(e.file, e.line): e for e in evidence}
    out: list[Citation] = []
    seen: set[tuple[str, int]] = set()

    def _push(file_: str, line: int, excerpt: str | None) -> None:
        key = (file_, line)
        if key in seen:
            return
        evi = by_anchor.get(key)
        out.append(
            Citation(
                id=evi.chunk_id if evi else None,
                file=file_,
                line=line,
                excerpt=(evi.excerpt if evi else (excerpt or "")),
            )
        )
        seen.add(key)

    for c in raw:
        try:
            file_ = str(c.get("file"))
            line = int(c.get("line"))
            _push(file_, line, c.get("excerpt"))
        except Exception:
            continue
    return out


def _empty_proposal(topic: str) -> SkillProposal:
    return SkillProposal(
        id=str(uuid.uuid4()),
        name=_normalise_name(topic),
        body=(
            "# (no proposal)\n\n"
            "The council could not gather enough evidence to draft a skill. "
            "Run `nexus ingest` against the relevant sources and try again."
        ),
        citations=[],
        confidence=0.0,
        status="pending",
        created_at=datetime.now(UTC).isoformat(),
    )


def _wrap_update(
    proposal: SkillProposal,
    model: str,
    *,
    prompt_tok: int,
    completion_tok: int,
    summary: str | None = None,
    is_redraft: bool = False,
) -> dict:
    return {
        "proposal": proposal,
        "proposal_id": proposal.id,
        "revision_count": 1 if is_redraft else 0,
        # Clear stale critique so the next Adversary pass starts fresh
        "critique": None,
        "deliberation": [
            DeliberationMessage(
                agent="synthesizer",
                timestamp=datetime.now(UTC).isoformat(),
                body=summary or f"Drafted proposal {proposal.id} (confidence {proposal.confidence:.2f}).",
                cite_ids=[c.id for c in proposal.citations if c.id],
            )
        ],
        "costs": [
            AgentCost(
                agent="synthesizer",
                prompt_tokens=prompt_tok,
                completion_tokens=completion_tok,
                model=model,
            )
        ],
    }


def _render_critique_block(crit, prior) -> str:
    issues = "\n".join(f"- {i.get('description','')}" for i in (crit.issues or []))
    return (
        "\n\n# Adversary critique (BLOCKING - you must address this)\n\n"
        f"Previous draft body:\n```\n{prior.body[:2000]}\n```\n\n"
        f"Critique:\n{issues}\n\n"
        f"Recommendation: {crit.recommendation}\n\n"
        "Address each issue in your redraft. Keep what works; replace what fails.\n"
    )
