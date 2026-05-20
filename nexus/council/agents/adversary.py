"""Adversary - red-team the Synthesizer's draft (ADR-007).

Reads the in-progress SkillProposal + the same evidence the Synthesizer saw.
Returns a Critique with severity (blocking | major | minor), issues list, and a
recommendation. Severity gate: only `blocking` triggers a redraft; max 1 cycle.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from nexus.config import NexusConfig
from nexus.council.state import (
    AgentCost,
    CouncilState,
    DeliberationMessage,
)
from nexus.llm.client import ChatClient
from nexus.skills.models import Critique, SkillProposal

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are the Adversary, an agent of the Nexus LLM Council. Your job is to "
    "find concrete failures in the draft skill BEFORE a human reviewer sees it. "
    "You are skeptical, specific, and bounded: never invent code that wasn't in "
    "the evidence; cite the file:line of any flaw you point out."
)


_USER_TEMPLATE = """Topic: {topic}
Skill kind: {skill_kind}

# Draft (under review)

{draft_body}

# Evidence the Synthesizer used

{evidence_block}

# Task

Find concrete weaknesses in the draft. For each, produce one issue object.
Classify the WORST issue's severity:

- **blocking**: the draft would mislead a future agent into producing wrong or
  unsafe code. The human reviewer must not be shown this without a redraft.
- **major**: meaningful weakness (missing edge case, unsupported claim, etc.)
  but the draft is still net-useful.
- **minor**: nits, wording, polish.

Output ONLY JSON in this schema:

{{
  "severity": "blocking" | "major" | "minor",
  "issues": [
    {{"description": "string", "counter_example": "optional code snippet or scenario"}}
  ],
  "recommendation": "1-3 sentence directive to the Synthesizer (only acted on if blocking)"
}}

Be concrete. If you have no real issues, return `"severity": "minor"` with an
empty `issues` array and a one-sentence recommendation.
"""


async def critique(
    *,
    proposal: SkillProposal,
    state: CouncilState,
    config: NexusConfig,
    chat: ChatClient,
) -> tuple[Critique, AgentCost, DeliberationMessage]:
    evidence_block = _render_evidence(state)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _USER_TEMPLATE.format(
                topic=state["topic"],
                skill_kind=state["skill_kind"],
                draft_body=proposal.body,
                evidence_block=evidence_block,
            ),
        },
    ]
    payload, usage = await chat.chat_json(messages, max_tokens=1500)
    severity = str(payload.get("severity", "minor")).lower()
    if severity not in ("blocking", "major", "minor"):
        severity = "minor"
    issues = [
        {
            "description": str(i.get("description", "")).strip(),
            "counter_example": str(i.get("counter_example", "")).strip() or None,
        }
        for i in (payload.get("issues") or [])
        if isinstance(i, dict) and i.get("description")
    ]
    crit = Critique(
        severity=severity,  # type: ignore[arg-type]
        issues=issues,
        recommendation=str(payload.get("recommendation", "")).strip(),
    )
    summary_body = _render_summary(crit)
    msg = DeliberationMessage(
        agent="adversary",
        timestamp=datetime.now(UTC).isoformat(),
        body=summary_body,
    )
    cost = AgentCost(
        agent="adversary",
        prompt_tokens=usage.prompt,
        completion_tokens=usage.completion,
        model=chat.model,
    )
    return crit, cost, msg


def _render_evidence(state: CouncilState) -> str:
    chunks = []
    code = state.get("code_patterns")
    if code:
        for p in code.patterns:
            chunks.extend(p.evidence)
    domain = state.get("domain_context")
    if domain:
        chunks.extend(domain.evidence)
    seen: set[str] = set()
    lines: list[str] = []
    for c in chunks:
        if c.chunk_id in seen:
            continue
        seen.add(c.chunk_id)
        lines.append(f"- [file: {c.file}:{c.line}] {c.excerpt[:160]}")
    return "\n".join(lines) or "(no evidence)"


def _render_summary(crit: Critique) -> str:
    lead = {
        "blocking": "🚨 BLOCKING critique - draft must be redrafted.",
        "major": "⚠️ Major critique attached (no redraft).",
        "minor": "Minor critique attached.",
    }[crit.severity]
    issue_lines = [f"  - {i['description']}" for i in crit.issues]
    parts = [lead]
    if issue_lines:
        parts.append("\nIssues:\n" + "\n".join(issue_lines))
    if crit.recommendation:
        parts.append(f"\nRecommendation: {crit.recommendation}")
    return "\n".join(parts)
