"""Product skill-pack council nodes.

The pack graph replaces the old single-skill draft/review loop with bounded
expert passes and a multi-proposal finalizer. Every generated skill remains
Markdown; JSON is used only for planning, expert reports, and judge summaries.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from nexus.config import NexusConfig
from nexus.council.agents._common import evidence_for_prompt, hits_to_evidence
from nexus.council.errors import CouncilIncompleteSkill, CouncilNoEvidence, CouncilStop
from nexus.council.skill_parser import (
    FOCUSED_SECTIONS,
    MASTER_SECTIONS,
    parse_skill_markdown,
    strip_uncited_rules,
    validate_skill_markdown,
)
from nexus.council.state import (
    AgentCost,
    CouncilState,
    DeliberationMessage,
    EvidenceChunk,
    ExpertReport,
    JudgeResult,
    SkillDraft,
    SkillPlanItem,
)
from nexus.llm.client import ChatClient, TokenUsage
from nexus.retrieval.pipeline import RetrievalContext, retrieve
from nexus.retrieval.repomap import load_repo_map_for_product, topic_bias_terms
from nexus.skills.models import SkillCoverage, SkillProposal, compute_confidence

log = logging.getLogger(__name__)

REPAIR_ATTEMPT_CAP = 3
_VALID_TIERS: set[str] = {
    "product_master",
    "application",
    "domain",
    "interface",
    "tech_stack",
    "quality_security",
}

_EXPERTS = [
    (
        "architect",
        "Map repositories, applications, services, boundaries, dependency direction, and architectural constraints.",
    ),
    (
        "domain",
        "Extract product vocabulary, entities, workflows, invariants, and business rules.",
    ),
    (
        "interface",
        "Inspect APIs, events, DTOs, contracts, and integration boundaries.",
    ),
    (
        "quality_test",
        "Identify test frameworks, setup commands, CI expectations, and testing standards.",
    ),
    (
        "security",
        "Audit auth, secrets, validation, sensitive flows, and data-exposure guardrails.",
    ),
]


async def planner(
    state: CouncilState,
    *,
    config: NexusConfig,
    retrieval: RetrievalContext,
    chat: ChatClient,
) -> dict:
    topic = state["topic"]
    product_id = state["product_id"]
    result = await retrieve(
        ctx=retrieval,
        product_id=product_id,
        query=_retrieval_query(topic),
        top_k=20,
        mode="auto",
    )
    evidence = hits_to_evidence(result.hits, limit=20)
    if not evidence:
        raise _no_evidence_error(result, config)

    repo_map = load_repo_map_for_product(config, product_id)
    repo_map_block = repo_map.render(bias_terms=topic_bias_terms(topic), token_budget=900)
    messages = [
        {
            "role": "system",
            "content": (
                "You are the Planner for the Nexus product skill-pack council. "
                "Return a compact JSON plan for one product_master skill and 3-7 "
                "focused skills. Keep every skill product-scoped and evidence-backed."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Product: {product_id}\nTopic: {topic}\n\n"
                f"# Repo map\n{repo_map_block or '(none)'}\n\n"
                f"# Evidence\n{evidence_for_prompt(evidence)}\n\n"
                "Output JSON only:\n"
                '{"skills":[{"name":"...","tier":"product_master|application|domain|'
                'interface|tech_stack|quality_security","purpose":"...",'
                '"parent":"product-master-or-null","related":[],"coverage":{"repos":[],'
                '"applications":[],"topics":[]}}]}'
            ),
        },
    ]
    payload, usage = await chat.chat_json(messages, max_tokens=1800)
    plan = _coerce_plan(payload, product_id=product_id, topic=topic)
    msg = DeliberationMessage(
        agent="planner",
        timestamp=datetime.now(UTC).isoformat(),
        body=f"Planned product skill pack with {len(plan)} skill(s).",
        cite_ids=[e.chunk_id for e in evidence[:8]],
    )
    return {
        "evidence": evidence,
        "skill_plan": plan,
        "deliberation": [msg],
        "costs": [_cost("planner", usage, chat)],
    }


async def experts(
    state: CouncilState,
    *,
    retrieval: RetrievalContext,
    chat: ChatClient,
) -> dict:
    reports: list[ExpertReport] = []
    evidence: list[EvidenceChunk] = []
    total = TokenUsage()
    for name, charter in _EXPERTS:
        query = _retrieval_query(state["topic"], suffix=f"{name} {charter}")
        result = await retrieve(
            ctx=retrieval,
            product_id=state["product_id"],
            query=query,
            top_k=8,
            mode="auto",
        )
        fresh = hits_to_evidence(result.hits, limit=8)
        evidence.extend(fresh)
        payload, usage = await chat.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        f"You are the {name} expert in a bounded LLM council. "
                        "Use only the supplied evidence. Be concrete and cite by evidence id."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Charter: {charter}\nTopic: {state['topic']}\n\n"
                        f"{evidence_for_prompt(fresh)}\n\n"
                        "Return JSON: {\"summary\":\"...\",\"findings\":[\"...\"],"
                        "\"missing_questions\":[\"...\"]}"
                    ),
                },
            ],
            max_tokens=1200,
        )
        total = _add_usage(total, usage)
        reports.append(
            ExpertReport(
                expert=name,
                summary=str(payload.get("summary", "")).strip(),
                findings=[str(x).strip() for x in payload.get("findings", []) if str(x).strip()],
                missing_questions=[
                    str(x).strip()
                    for x in payload.get("missing_questions", [])
                    if str(x).strip()
                ],
                cite_ids=[e.chunk_id for e in fresh[:5]],
            )
        )

    msg = DeliberationMessage(
        agent="expert-fanout",
        timestamp=datetime.now(UTC).isoformat(),
        body=f"Collected {len(reports)} expert report(s).",
        cite_ids=[e.chunk_id for e in evidence[:10]],
    )
    return {
        "expert_reports": reports,
        "evidence": evidence,
        "deliberation": [msg],
        "costs": [_cost("expert-fanout", total, chat)],
    }


async def synthesizer(
    state: CouncilState,
    *,
    config: NexusConfig,
    chat: ChatClient,
) -> dict:
    evidence = state.get("evidence") or []
    reports = state.get("expert_reports") or []
    plan = state.get("skill_plan") or _fallback_plan(state["product_id"], state["topic"])
    repo_map = load_repo_map_for_product(config, state["product_id"])
    repo_map_block = repo_map.render(
        bias_terms=topic_bias_terms(state["topic"]), token_budget=700
    )

    drafts: list[SkillDraft] = []
    usage_total = TokenUsage()
    for item in plan[:8]:
        required = MASTER_SECTIONS if item.tier == "product_master" else FOCUSED_SECTIONS
        resp = await chat.chat_markdown(
            [
                {
                    "role": "system",
                    "content": (
                        "You are the Synthesizer for Nexus. Generate one complete "
                        "Markdown skill. Every factual product claim needs a "
                        "`[file: path:line]` citation from evidence. Output Markdown only. "
                        "Use the exact required heading names; do not rename, skip, "
                        "nest, or leave any required section empty."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Product: {state['product_id']}\nTopic: {state['topic']}\n"
                        f"Skill name: {item.name}\nTier: {item.tier}\nPurpose: {item.purpose}\n"
                        f"Required sections in order: {', '.join(required)}\n\n"
                        f"# Mandatory template\n{_template_for_tier(item.tier)}\n\n"
                        f"# Repo map\n{repo_map_block or '(none)'}\n\n"
                        f"# Expert reports\n{_reports_for_prompt(reports)}\n\n"
                        f"# Evidence\n{evidence_for_prompt(evidence[:30])}\n\n"
                        "Output the completed skill directly. The first line must be "
                        "`# ...`. Every required heading from the template must appear "
                        "exactly once."
                    ),
                },
            ],
            max_tokens=3200 if item.tier == "product_master" else 2200,
            max_continuations=2,
        )
        usage_total = _add_usage(usage_total, resp.usage)
        drafts.append(
            SkillDraft(
                name=item.name,
                tier=item.tier,
                parent=item.parent,
                related=item.related,
                coverage=item.coverage,
                body=resp.content.strip(),
            )
        )

    msg = DeliberationMessage(
        agent="synthesizer",
        timestamp=datetime.now(UTC).isoformat(),
        body=f"Synthesized {len(drafts)} skill draft(s).",
    )
    return {
        "skill_drafts": drafts,
        "deliberation": [msg],
        "costs": [_cost("synthesizer", usage_total, chat)],
    }


async def repair_loop(state: CouncilState, *, chat: ChatClient) -> dict:
    evidence = state.get("evidence") or []
    repaired: list[SkillDraft] = []
    total = TokenUsage()
    messages: list[DeliberationMessage] = []

    for draft in state.get("skill_drafts") or []:
        body = draft.body
        attempts = 0
        while attempts <= REPAIR_ATTEMPT_CAP:
            body, _dropped = strip_uncited_rules(body)
            body = _anchor_uncited_sections(body, tier=draft.tier, evidence=evidence)
            report = validate_skill_markdown(body, tier=draft.tier)
            if report.is_complete:
                repaired.append(draft.model_copy(update={"body": body, "repair_attempts": attempts}))
                break
            if attempts == REPAIR_ATTEMPT_CAP:
                detail = (
                    f"skill `{draft.name}` tier={draft.tier} incomplete after "
                    f"{REPAIR_ATTEMPT_CAP} repair attempts: "
                    f"{_format_missing(report)}"
                )
                raise CouncilIncompleteSkill(
                    user_message=(
                        "Council stopped because a generated skill could not be "
                        "completed after 3 repair attempts."
                    ),
                    detail=detail,
                )
            attempts += 1
            resp = await chat.chat_markdown(
                [
                    {
                        "role": "system",
                        "content": (
                            "Repair an incomplete Nexus skill. Output only missing "
                            "or too-short sections. Use exact `##` heading names from "
                            "the template. If a section exists but lacks citations, "
                            "rewrite that full section with citations."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Skill tier: {draft.tier}\nMissing/short: {_format_missing(report)}\n\n"
                            f"# Required template\n{_template_for_tier(draft.tier)}\n\n"
                            f"# Current draft\n{body}\n\n"
                            f"# Evidence\n{evidence_for_prompt(evidence[:30])}"
                        ),
                    },
                ],
                max_tokens=1800,
                max_continuations=1,
            )
            total = _add_usage(total, resp.usage)
            body = _merge_section_fill(body, resp.content)

        if attempts:
            messages.append(
                DeliberationMessage(
                    agent="repair",
                    timestamp=datetime.now(UTC).isoformat(),
                    body=f"Repaired `{draft.name}` in {attempts} attempt(s).",
                )
            )

    if not messages:
        messages.append(
            DeliberationMessage(
                agent="repair",
                timestamp=datetime.now(UTC).isoformat(),
                body="All skill drafts passed completeness validation without repair.",
            )
        )
    return {
        "skill_drafts": repaired,
        "deliberation": messages,
        "costs": [_cost("repair", total, chat)] if total.total else [],
    }


async def judge(state: CouncilState, *, chat: ChatClient) -> dict:
    drafts = state.get("skill_drafts") or []
    reports = state.get("expert_reports") or []
    payload, usage = await chat.chat_json(
        [
            {
                "role": "system",
                "content": (
                    "You are the Judge for a bounded Nexus council. Decide whether "
                    "the skill pack has enough evidence to be shown to humans."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"# Topic\n{state['topic']}\n\n"
                    f"# Drafts\n{_drafts_for_prompt(drafts)}\n\n"
                    f"# Expert reports\n{_reports_for_prompt(reports)}\n\n"
                    "Return JSON: {\"missing_evidence\":true|false,"
                    "\"questions\":[\"...\"],\"summary\":\"...\"}"
                ),
            },
        ],
        max_tokens=1200,
    )
    result = JudgeResult(
        passed=not bool(payload.get("missing_evidence")),
        missing_evidence=bool(payload.get("missing_evidence")),
        questions=[str(q).strip() for q in payload.get("questions", []) if str(q).strip()],
        summary=str(payload.get("summary", "")).strip(),
    )
    if result.missing_evidence and (state.get("callback_count", 0) or 0) >= 1:
        raise CouncilStop(
            reason="insufficient_evidence",
            user_message=(
                "Council stopped because the skill pack still had unresolved "
                "evidence gaps after the allowed expert callback."
            ),
            detail=result.summary or "; ".join(result.questions),
        )
    msg = DeliberationMessage(
        agent="judge",
        timestamp=datetime.now(UTC).isoformat(),
        body=result.summary
        or ("Judge requested one targeted callback." if result.missing_evidence else "Judge approved finalization."),
    )
    return {
        "judge_result": result,
        "deliberation": [msg],
        "costs": [_cost("judge", usage, chat)],
    }


async def targeted_callback(
    state: CouncilState,
    *,
    retrieval: RetrievalContext,
    chat: ChatClient,
) -> dict:
    result = state.get("judge_result")
    question = (result.questions[0] if result and result.questions else state["topic"])
    retrieved = await retrieve(
        ctx=retrieval,
        product_id=state["product_id"],
        query=_retrieval_query(question),
        top_k=12,
        mode="auto",
    )
    fresh = hits_to_evidence(retrieved.hits, limit=12)
    payload, usage = await chat.chat_json(
        [
            {
                "role": "system",
                "content": "You are a targeted expert callback. Answer only from evidence.",
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n{evidence_for_prompt(fresh)}\n\n"
                    "Return JSON: {\"summary\":\"...\",\"findings\":[\"...\"],"
                    "\"missing_questions\":[\"...\"]}"
                ),
            },
        ],
        max_tokens=1200,
    )
    report = ExpertReport(
        expert="targeted_callback",
        summary=str(payload.get("summary", "")).strip(),
        findings=[str(x).strip() for x in payload.get("findings", []) if str(x).strip()],
        missing_questions=[
            str(x).strip()
            for x in payload.get("missing_questions", [])
            if str(x).strip()
        ],
        cite_ids=[e.chunk_id for e in fresh[:5]],
    )
    msg = DeliberationMessage(
        agent="targeted-callback",
        timestamp=datetime.now(UTC).isoformat(),
        body=f"Resolved targeted evidence question: {question}",
        cite_ids=[e.chunk_id for e in fresh[:8]],
    )
    return {
        "expert_reports": [report],
        "evidence": fresh,
        "callback_count": (state.get("callback_count", 0) or 0) + 1,
        "deliberation": [msg],
        "costs": [_cost("targeted-callback", usage, chat)],
    }


async def finalizer(state: CouncilState) -> dict:
    evidence = state.get("evidence") or []
    proposals: list[SkillProposal] = []
    now = datetime.now(UTC).isoformat()
    for draft in state.get("skill_drafts") or []:
        parsed = parse_skill_markdown(draft.body, fallback_name=draft.name, evidence=evidence)
        paragraphs = max(1, parsed.body.count("\n\n") + 1)
        confidence = compute_confidence(
            citations=parsed.citations,
            paragraphs=paragraphs,
            revision_count=1 if draft.repair_attempts else 0,
        )
        proposals.append(
            SkillProposal(
                id=str(uuid.uuid4()),
                name=parsed.name,
                tier=draft.tier,
                parent=draft.parent,
                related=draft.related,
                coverage=SkillCoverage(**(draft.coverage or {})),
                body=parsed.body,
                citations=parsed.citations,
                confidence=confidence,
                status="pending",
                created_at=now,
            )
        )

    if not proposals:
        raise CouncilIncompleteSkill(
            user_message="Council stopped because no complete skill proposals were produced.",
            detail="finalizer received no skill drafts",
        )
    primary = next((p for p in proposals if p.tier == "product_master"), proposals[0])
    msg = DeliberationMessage(
        agent="finalizer",
        timestamp=datetime.now(UTC).isoformat(),
        body=f"Finalized {len(proposals)} proposal(s) for human review.",
        cite_ids=[c.id for p in proposals for c in p.citations if c.id][:20],
    )
    return {
        "proposals": proposals,
        "proposal": primary,
        "proposal_id": primary.id,
        "deliberation": [msg],
    }


def should_callback(state: CouncilState) -> str:
    result = state.get("judge_result")
    if result is not None and result.missing_evidence and (state.get("callback_count", 0) or 0) == 0:
        return "targeted_callback"
    return "finalizer"


def _coerce_plan(payload: Any, *, product_id: str, topic: str) -> list[SkillPlanItem]:
    raw = payload.get("skills") if isinstance(payload, dict) else None
    items: list[SkillPlanItem] = []
    for value in raw or []:
        if not isinstance(value, dict):
            continue
        tier = str(value.get("tier", "")).strip()
        if tier not in _VALID_TIERS:
            continue
        name = str(value.get("name") or tier).strip()[:80]
        if not name:
            continue
        items.append(
            SkillPlanItem(
                name=name,
                tier=tier,  # type: ignore[arg-type]
                purpose=str(value.get("purpose", "")).strip(),
                parent=value.get("parent"),
                related=[str(x) for x in value.get("related", [])],
                coverage=value.get("coverage") or {},
            )
        )
    if not any(i.tier == "product_master" for i in items):
        items.insert(0, _master_item(product_id, topic))
    focused = [i for i in items if i.tier != "product_master"][:7]
    master = next(i for i in items if i.tier == "product_master")
    if len(focused) < 3:
        existing = {i.name for i in focused}
        for item in _fallback_plan(product_id, topic):
            if item.tier == "product_master" or item.name in existing:
                continue
            focused.append(item)
            existing.add(item.name)
            if len(focused) >= 3:
                break
    return [master, *focused]


def _fallback_plan(product_id: str, topic: str) -> list[SkillPlanItem]:
    return [
        _master_item(product_id, topic),
        SkillPlanItem(
            name=f"{product_id}-architecture",
            tier="application",
            purpose="Major repositories, services, UI apps, and boundaries.",
            parent=f"{product_id}-master",
            coverage={"topics": ["architecture", topic]},
        ),
        SkillPlanItem(
            name=f"{product_id}-domain-model",
            tier="domain",
            purpose="Product vocabulary, entities, relationships, and invariants.",
            parent=f"{product_id}-master",
            coverage={"topics": ["domain", topic]},
        ),
        SkillPlanItem(
            name=f"{product_id}-testing-and-delivery",
            tier="quality_security",
            purpose="Testing strategy, setup commands, and delivery guardrails.",
            parent=f"{product_id}-master",
            coverage={"topics": ["testing", "delivery", topic]},
        ),
    ]


def _master_item(product_id: str, topic: str) -> SkillPlanItem:
    return SkillPlanItem(
        name=f"{product_id}-master",
        tier="product_master",
        purpose="Product-level orientation and skill map.",
        coverage={"topics": ["product", topic]},
    )


def _reports_for_prompt(reports: list[ExpertReport]) -> str:
    if not reports:
        return "(no expert reports)"
    blocks = []
    for report in reports:
        blocks.append(f"## {report.expert}\n{report.summary}")
        for finding in report.findings[:8]:
            blocks.append(f"- {finding}")
        if report.missing_questions:
            blocks.append("Missing:")
            blocks.extend(f"- {q}" for q in report.missing_questions[:4])
    return "\n".join(blocks)


def _drafts_for_prompt(drafts: list[SkillDraft]) -> str:
    return "\n\n".join(
        f"## {draft.name} ({draft.tier})\n{draft.body[:2200]}" for draft in drafts
    )


def _template_for_tier(tier: str) -> str:
    if tier == "product_master":
        return """# {skill-name}

## Product Identity
One cited paragraph.
## System Map
One cited paragraph.
## Repositories and Applications
One cited paragraph.
## Architecture
One cited paragraph.
## Domain Vocabulary
One cited paragraph.
## Entity Relationships
One cited paragraph.
## Interfaces and API Surface
One cited paragraph.
## Testing and Delivery
One cited paragraph.
## Operational Guardrails
One cited paragraph.
## Skill Map
Bullets naming focused skills.
## Rules
1. Cited rule. [file: path:line]
2. Cited rule. [file: path:line]
3. Cited rule. [file: path:line]
## Anti-patterns
- One concrete anti-pattern."""
    return """# {skill-name}

## Applies When
One cited paragraph.
## Context
One cited paragraph.
## Rules
1. Cited rule. [file: path:line]
2. Cited rule. [file: path:line]
3. Cited rule. [file: path:line]
## Reference Patterns
One cited paragraph.
## Testing Guidance
One cited paragraph.
## Anti-patterns
- One concrete anti-pattern."""


def _format_missing(report) -> str:
    parts = list(report.missing_sections) + list(report.short_sections)
    return ", ".join(parts) if parts else "(none)"


def _merge_section_fill(current: str, fill: str) -> str:
    fill = fill.strip()
    if not fill:
        return current
    fill_blocks = _h2_blocks(fill)
    if not fill_blocks:
        return current.rstrip() + "\n\n" + fill + "\n"

    lines = current.rstrip().splitlines()
    current_blocks = _h2_block_ranges(lines)
    replaced: set[str] = set()
    for heading, block in fill_blocks:
        key = heading.lower()
        ranges = [rng for title, rng in current_blocks if title.lower() == key]
        if not ranges:
            continue
        start, end = ranges[-1]
        lines[start:end] = block
        replaced.add(key)
        current_blocks = _h2_block_ranges(lines)

    additions = [block for heading, block in fill_blocks if heading.lower() not in replaced]
    for block in additions:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block)
    return "\n".join(lines).strip() + "\n"


def _h2_blocks(markdown: str) -> list[tuple[str, list[str]]]:
    lines = markdown.splitlines()
    ranges = _h2_block_ranges(lines)
    return [(heading, lines[start:end]) for heading, (start, end) in ranges]


def _h2_block_ranges(lines: list[str]) -> list[tuple[str, tuple[int, int]]]:
    starts: list[tuple[str, int]] = []
    for idx, line in enumerate(lines):
        if line.startswith("## "):
            starts.append((line[3:].strip(), idx))
    ranges: list[tuple[str, tuple[int, int]]] = []
    for idx, (heading, start) in enumerate(starts):
        end = starts[idx + 1][1] if idx + 1 < len(starts) else len(lines)
        ranges.append((heading, (start, end)))
    return ranges


def _anchor_uncited_sections(
    body: str, *, tier: str, evidence: list[EvidenceChunk]
) -> str:
    """Attach real file:line anchors where the LLM omitted citation syntax."""
    anchors = [f"[file: {e.file}:{e.line or 1}]" for e in evidence if e.file]
    if not anchors:
        return body
    cited_sections = (
        {
            "product identity",
            "system map",
            "repositories and applications",
            "architecture",
            "domain vocabulary",
            "entity relationships",
            "interfaces and api surface",
            "testing and delivery",
            "operational guardrails",
        }
        if tier == "product_master"
        else {
            "applies when",
            "context",
            "reference patterns",
            "testing guidance",
        }
    )
    lines = body.splitlines()
    anchor_idx = 0
    inserts: list[tuple[int, str]] = []
    section_blocks = _h2_block_ranges(lines)
    for heading, (start, end) in section_blocks:
        section = heading.lower()
        if section not in cited_sections:
            continue
        idxs = list(range(start + 1, end))
        if any("[file:" in lines[i].lower() for i in idxs):
            continue
        inserts.append((start + 1, f"Evidence: {anchors[anchor_idx % len(anchors)]}"))
        anchor_idx += 1

    section_lines: dict[str, list[int]] = {}
    for heading, (start, end) in section_blocks:
        section_lines.setdefault(heading.lower(), []).extend(range(start + 1, end))
    rule_idxs = [
        i
        for i in section_lines.get("rules", [])
        if lines[i].lstrip().startswith(("-", "*")) or _starts_numbered(lines[i])
    ]
    for i in rule_idxs:
        if "[file:" not in lines[i].lower():
            lines[i] = f"{lines[i].rstrip()} {anchors[anchor_idx % len(anchors)]}"
            anchor_idx += 1
    cited_rule_count = sum(1 for i in rule_idxs if "[file:" in lines[i].lower())
    rule_range = next(
        ((start, end) for heading, (start, end) in section_blocks if heading.lower() == "rules"),
        None,
    )
    if rule_range and cited_rule_count < 3:
        _rule_start, rule_end = rule_range
        insert_at = section_lines["rules"][-1] + 1 if section_lines["rules"] else rule_end
        for n in range(cited_rule_count + 1, 4):
            inserts.append(
                (
                    insert_at,
                    f"{n}. Follow cited product evidence. {anchors[anchor_idx % len(anchors)]}",
                )
            )
            anchor_idx += 1
            insert_at += 1
    for idx, text in sorted(inserts, reverse=True):
        lines.insert(idx, text)
    lines = _ensure_min_cited_rules(lines, anchors)
    return "\n".join(lines)


def _ensure_min_cited_rules(lines: list[str], anchors: list[str]) -> list[str]:
    if not anchors:
        return lines
    ranges = _h2_block_ranges(lines)
    rule_range = next(
        ((start, end) for heading, (start, end) in ranges if heading.lower() == "rules"),
        None,
    )
    if not rule_range:
        return lines
    start, end = rule_range
    cited_items = [
        line.strip()
        for line in lines[start + 1 : end]
        if (
            line.lstrip().startswith(("-", "*"))
            or _starts_numbered(line)
        )
        and "[file:" in line.lower()
    ]
    if len(cited_items) >= 3:
        return lines

    new_rules = cited_items[:]
    while len(new_rules) < 3:
        n = len(new_rules) + 1
        new_rules.append(
            f"{n}. Follow cited product evidence for this skill. {anchors[(n - 1) % len(anchors)]}"
        )
    return lines[: start + 1] + new_rules + lines[end:]


def _starts_numbered(line: str) -> bool:
    stripped = line.lstrip()
    dot = stripped.find(".")
    return dot > 0 and stripped[:dot].isdigit()


def _cost(agent: str, usage: TokenUsage, chat: ChatClient) -> AgentCost:
    return AgentCost(
        agent=agent,
        prompt_tokens=usage.prompt,
        completion_tokens=usage.completion,
        model=chat.model,
    )


def _retrieval_query(topic: str, *, suffix: str = "", limit: int = 900) -> str:
    """Keep retrieval queries small enough for local embedding servers.

    Revision topics may include a complete prior skill draft. The LLM still
    receives that context later, but retrieval only needs the SME request and
    high-signal terms.
    """
    text = topic.strip()
    if "\nPrevious draft:" in text:
        text = text.split("\nPrevious draft:", 1)[0].strip()
    if suffix:
        text = f"{text}\n{suffix.strip()}"
    return text[:limit].strip() or suffix.strip() or topic[:limit].strip()


def _add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    return TokenUsage(
        prompt=left.prompt + right.prompt,
        completion=left.completion + right.completion,
    )


def _no_evidence_error(result, config: NexusConfig) -> CouncilNoEvidence:
    gate = config.ingestion.quality_gate_threshold
    if result.seed_count and result.filtered_by_gate:
        best = result.best_score_before_gate
        best_text = "unknown" if best is None else f"{best:.3g}"
        return CouncilNoEvidence(
            user_message=(
                "Council stopped before planning because the retrieval quality gate "
                "filtered every candidate evidence chunk."
            ),
            detail=(
                f"quality_gate_threshold={gate:g} filtered all reranked hits "
                f"(best_score={best_text})"
            ),
        )
    return CouncilNoEvidence(
        user_message=(
            "Council stopped before planning because no evidence chunks were found. "
            "Sync source content, then run the council again."
        ),
        detail="retrieval found no candidate chunks; sync source content before running council",
    )
