"""Markdown skill parser + completeness validator tests (no LLM calls)."""

from types import SimpleNamespace

import pytest

from nexus.council.agents.skill import (  # private guardrails
    _align_citations_to_evidence,
    _anchor_uncited_sections,
    repair_loop,
)
from nexus.council.errors import CouncilIncompleteSkill
from nexus.council.skill_parser import (
    _normalise_name,  # private but stable for tests
    parse_skill_markdown,
    required_sections_for_tier,
    strip_uncited_rules,
    validate_completeness,
    validate_skill_markdown,
)
from nexus.council.state import EvidenceChunk, SkillDraft
from nexus.llm.client import ChatResponse, TokenUsage


def _evi() -> list[EvidenceChunk]:
    return [
        EvidenceChunk(chunk_id="c1", file="a.rs", line=10, score=0.9, excerpt="x"),
        EvidenceChunk(chunk_id="c2", file="b.rs", line=20, score=0.8, excerpt="y"),
    ]


# ---- strip_uncited_rules ---------------------------------------------------


def test_strip_keeps_cited_rule_items() -> None:
    body = (
        "# Title\n\nIntro paragraph.\n\n"
        "## Rules\n\n"
        "1. Cited rule [file: a.rs:10].\n"
        "2. Another cited rule [file: b.rs:20].\n"
    )
    out, dropped = strip_uncited_rules(body)
    assert dropped == 0
    assert out == body


def test_strip_removes_uncited_rule_items() -> None:
    body = (
        "# Title\n\n"
        "## Rules\n\n"
        "1. Cited rule [file: a.rs:10].\n"
        "2. Uncited claim without anchor.\n"
        "3. Another cited rule [file: b.rs:20].\n"
    )
    out, dropped = strip_uncited_rules(body)
    assert dropped == 1
    assert "Uncited claim" not in out
    assert "Cited rule" in out


def test_strip_leaves_prose_outside_rules_alone() -> None:
    body = (
        "# Title\n\nFree-form intro with no citation. Still fine.\n\n"
        "## Rules\n\n"
        "1. Cited [file: a.rs:10].\n\n"
        "## Anti-patterns\n\n"
        "- An uncited anti-pattern, allowed in this section.\n"
    )
    out, dropped = strip_uncited_rules(body)
    assert dropped == 0
    assert "Free-form intro" in out
    assert "uncited anti-pattern" in out


# ---- _normalise_name --------------------------------------------------------


def test_normalise_name_kebab_cases_and_caps_length() -> None:
    assert _normalise_name("PDA Seed Validation") == "pda-seed-validation"
    assert _normalise_name("snake_case_input") == "snake-case-input"
    assert _normalise_name("--leading--and--trailing--") == "leading-and-trailing"
    long_name = "x" * 200
    assert len(_normalise_name(long_name)) <= 60


# ---- parse_skill_markdown ---------------------------------------------------


def test_parse_extracts_name_body_and_citations() -> None:
    md = (
        "# auth-token-rotation\n\n"
        "Intro.\n\n"
        "## Rules\n"
        "1. Do X [file: a.rs:10].\n"
        "2. Do Y [file: b.rs:20].\n"
    )
    parsed = parse_skill_markdown(md, evidence=_evi())
    assert parsed.name == "auth-token-rotation"
    assert "## Rules" in parsed.body
    assert {c.file for c in parsed.citations} == {"a.rs", "b.rs"}
    # excerpt populated from evidence pool when anchor matches
    assert any(c.excerpt for c in parsed.citations)


def test_parse_accepts_github_uri_citations() -> None:
    evidence = [
        EvidenceChunk(
            chunk_id="gh1",
            file="github:org/repo/nexus/api/routes/council.py",
            line=42,
            score=0.9,
            excerpt="route",
        )
    ]
    md = "# Demo\n\n## Rules\n1. Use route [file: github:org/repo/nexus/api/routes/council.py:42]"

    parsed = parse_skill_markdown(md, evidence=evidence)

    assert parsed.citations[0].file == "github:org/repo/nexus/api/routes/council.py"
    assert parsed.citations[0].id == "gh1"


def test_parse_falls_back_when_no_h1() -> None:
    parsed = parse_skill_markdown("body only", fallback_name="My Topic")
    assert parsed.name == "my-topic"


def test_parse_dedupes_citations() -> None:
    md = "# t\n\n[file: a.rs:10] mentioned twice [file: a.rs:10]."
    parsed = parse_skill_markdown(md, evidence=_evi())
    assert len(parsed.citations) == 1


# ---- validate_completeness --------------------------------------------------


def test_complete_skill_reports_complete() -> None:
    md = (
        "# t\n\nIntro.\n\n"
        "## Rules\n"
        "1. r1 [file: a.rs:10]\n"
        "2. r2 [file: a.rs:11]\n"
        "3. r3 [file: a.rs:12]\n\n"
        "## Anti-patterns\n"
        "- avoid X\n"
    )
    report = validate_completeness(md)
    assert report.is_complete


def test_validate_flags_missing_anti_patterns() -> None:
    md = (
        "# t\n\nIntro.\n\n"
        "## Rules\n"
        "1. r1 [file: a.rs:10]\n"
        "2. r2 [file: a.rs:11]\n"
        "3. r3 [file: a.rs:12]\n"
    )
    report = validate_completeness(md)
    assert not report.is_complete
    assert "anti-patterns" in report.missing_sections


def test_validate_flags_too_few_rules() -> None:
    md = (
        "# t\n\n"
        "## Rules\n"
        "1. r1 [file: a.rs:10]\n\n"
        "## Anti-patterns\n"
        "- x\n"
    )
    report = validate_completeness(md)
    assert not report.is_complete
    assert any("rules" in s for s in report.short_sections)


def test_validate_flags_missing_title() -> None:
    md = (
        "no heading\n\n"
        "## Rules\n"
        "1. r1 [file: a.rs:10]\n"
        "2. r2 [file: b.rs:20]\n"
        "3. r3 [file: a.rs:11]\n\n"
        "## Anti-patterns\n- x\n"
    )
    report = validate_completeness(md)
    assert "title" in report.missing_sections


# sections that require ≥2 bullet/numbered items in the new validator
_ENUMERABLE = {
    "capabilities and workflows",
    "system map",
    "data model",
    "interfaces and contracts",
    "invariants and constraints",
    "security and secrets",
    "known traps",
    "freshness and evidence",
    "product language",
}


def _product_skill_body(
    *,
    omit: str | None = None,
    uncited: str | None = None,
    file: str = "a.rs",
    line: int = 10,
) -> str:
    lines = ["# product-skill", ""]
    for section in required_sections_for_tier("product_master"):
        if section == omit:
            continue
        lines.append(f"## {section}")
        if section == "Use This Skill When":
            lines.append("Use for product orientation and grounded development.")
        elif section == "How To Use The Knowledge Base":
            lines.append("Query KB/RAG for fresh source-of-truth details before concrete changes.")
        elif section == "How To Work In This Product":
            lines.append("Read local context, edit narrowly, and verify with project checks.")
        elif section in {"Product Snapshot"}:
            # short prose is fine for snapshot
            if section == uncited:
                lines.append(f"Evidence supports {section.lower()}.")
            else:
                lines.append(f"Evidence supports {section.lower()} [file: {file}:{line}].")
        elif section.lower() in _ENUMERABLE:
            # emit ≥2 list items so the new MIN_LIST check passes
            if section == uncited:
                lines.append(f"- Evidence for {section.lower()} item one.")
                lines.append(f"- Evidence for {section.lower()} item two.")
            else:
                lines.append(f"- Evidence for {section.lower()} item one [file: {file}:{line}].")
                lines.append(f"- Evidence for {section.lower()} item two [file: {file}:{line}].")
        else:
            if section == uncited:
                lines.append(f"Evidence supports {section.lower()}.")
            else:
                lines.append(f"Evidence supports {section.lower()} [file: {file}:{line}].")
        lines.append("")
    return "\n".join(lines)


def test_validate_product_master_requires_locked_sections_and_citations() -> None:
    assert validate_skill_markdown(_product_skill_body(), tier="product_master").is_complete


def test_validate_product_skill_rejects_unknown_legacy_shape() -> None:
    md = _product_skill_body() + "\n## Legacy Heading\nOld skill section.\n"
    report = validate_skill_markdown(md, tier="application")
    assert not report.is_complete
    assert any("unexpected sections" in item for item in report.short_sections)


def test_validate_focused_skill_rejects_uncited_factual_section() -> None:
    md = _product_skill_body(uncited="Interfaces And Contracts")
    report = validate_skill_markdown(md, tier="application")
    assert not report.is_complete
    assert "interfaces and contracts (needs citation)" in report.short_sections


def test_validate_engineering_allows_uncited_procedural_sections() -> None:
    assert validate_skill_markdown(_product_skill_body(), tier="quality_security").is_complete


def test_align_citations_rewrites_anchors_missing_from_visible_evidence() -> None:
    body = _product_skill_body(file="missing.py", line=99)

    aligned, issues = _align_citations_to_evidence(
        body,
        tier="product_master",
        evidence=[
            EvidenceChunk(
                chunk_id="visible",
                file="a.rs",
                line=10,
                score=0.9,
                excerpt="visible evidence",
            )
        ],
    )

    assert issues == []
    assert "missing.py:99" not in aligned
    assert "[file: a.rs:10]" in aligned


def test_validate_procedural_section_rejects_uncited_concrete_claim() -> None:
    md = _product_skill_body().replace(
        "Read local context, edit narrowly, and verify with project checks.",
        "Run `uv run pytest -q` before changing code.",
    )
    report = validate_skill_markdown(md, tier="quality_security")
    assert not report.is_complete
    assert "how to work in this product (concrete claim needs citation)" in report.short_sections


def test_anchor_guardrail_no_longer_stuffs_citations() -> None:
    md = (
        "# Architecture Skill\n\n"
        "## Use This Skill When\nUse for API changes.\n"
        "## API and MCP Surface\nThe service exposes routes.\n"
    )
    anchored = _anchor_uncited_sections(md, tier="application", evidence=_evi())
    assert anchored == md
    assert not validate_skill_markdown(anchored, tier="application").is_complete
    assert "Follow cited product evidence" not in anchored


class _RepairChat:
    model = "test"

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.prompts: list[str] = []

    async def chat_markdown(self, messages, **kwargs):
        self.prompts.append(messages[-1]["content"])
        content = self.responses.pop(0) if self.responses else ""
        return ChatResponse(content=content, usage=TokenUsage(prompt=1, completion=1), model="test")


@pytest.mark.asyncio
async def test_repair_loop_prompts_for_specific_missing_section() -> None:
    body = _product_skill_body(omit="Known Traps")
    chat = _RepairChat([
        "## Known Traps\n"
        "- Do not bypass validation [file: a.rs:10].\n"
        "- Do not skip the approval step [file: a.rs:10].\n"
    ])

    out = await repair_loop(
        {
            "evidence": _evi(),
            "skill_drafts": [
                SkillDraft(name="product-skill", tier="application", body=body)
            ],
        },
        chat=chat,
    )

    assert "Add only the missing `## Known Traps` section" in chat.prompts[0]
    assert len(out["skill_drafts"]) == 1
    assert out["skill_drafts"][0].repair_warnings == []
    assert "## Known Traps" in out["skill_drafts"][0].body


@pytest.mark.asyncio
async def test_repair_loop_keeps_reviewable_draft_with_warning() -> None:
    body = "# product-skill\n\n## Interfaces And Contracts\nRoutes [file: a.rs:10].\n"
    chat = _RepairChat([""] * 4)

    with pytest.raises(CouncilIncompleteSkill):
        await repair_loop(
            {
                "evidence": _evi(),
                "skill_drafts": [
                    SkillDraft(name="product-skill", tier="application", body=body)
                ],
            },
            chat=chat,
        )


class _ChunkGrepIndexer:
    async def iter_chunk_payloads(self, *, product_id, vector_kind, batch_size):
        if vector_kind != "code":
            return
        yield "arch-c1", {
            "resource_uri": "a.py",
            "start_line": 10,
            "context_path": "CouncilGraph",
            "content": (
                "Architecture runtime repositories applications boundaries ownership "
                "extension points gotchas validation checklist anti-patterns."
            ),
        }


class _SampleOnlyIndexer:
    async def iter_chunk_payloads(self, *, product_id, vector_kind, batch_size):
        if vector_kind != "code":
            return
        yield "fallback-c1", {
            "resource_uri": "github:org/repo/pyproject.toml",
            "start_line": 1,
            "context_path": "",
            "content": "[tool.pytest.ini_options]\naddopts = \"-q\"\n",
        }


@pytest.mark.asyncio
async def test_repair_loop_anchors_many_uncited_sections_with_chunk_grep() -> None:
    sections = list(required_sections_for_tier("product_master"))
    body = "# arch\n\n" + "\n".join(
        f"## {section}\n- Architecture facts need grounding.\n- More architecture facts."
        for section in sections
    )
    repaired_sections = "\n".join(
        (
            f"## {section}\n"
            f"- Product facts are grounded in indexed chunks [file: a.py:10].\n"
            f"- More product facts grounded in indexed chunks [file: a.py:10]."
        )
        for section in sections
        if section
        not in {"Use This Skill When", "How To Use The Knowledge Base", "How To Work In This Product"}
    )
    chat = _RepairChat([repaired_sections])

    out = await repair_loop(
        {
            "product_id": "demo",
            "topic": "architecture",
            "evidence": [],
            "skill_drafts": [
                SkillDraft(name="product-skill", tier="application", body=body)
            ],
        },
        chat=chat,
        retrieval=SimpleNamespace(indexer=_ChunkGrepIndexer()),
    )

    assert chat.prompts == []
    draft = out["skill_drafts"][0]
    assert draft.repair_attempts == 0
    assert "## System Map" in draft.body
    assert "a.py:10" in draft.body


@pytest.mark.asyncio
async def test_repair_loop_anchors_quality_sections_from_sampled_chunks() -> None:
    sections = list(required_sections_for_tier("product_master"))
    body = "# quality\n\n" + "\n".join(
        f"## {section}\n- Keep work grounded.\n- Also keep work grounded." for section in sections
    )
    chat = _RepairChat([""] * 4)

    out = await repair_loop(
        {
            "product_id": "demo",
            "topic": "zzzz unmatched",
            "evidence": [],
            "skill_drafts": [
                SkillDraft(name="product-skill", tier="quality_security", body=body)
            ],
        },
        chat=chat,
        retrieval=SimpleNamespace(indexer=_SampleOnlyIndexer()),
    )

    assert chat.prompts == []
    draft = out["skill_drafts"][0]
    assert draft.repair_attempts == 0
    assert "[file: github:org/repo/pyproject.toml:1]" in draft.body


@pytest.mark.asyncio
async def test_repair_loop_replaces_fabricated_citations_before_eval() -> None:
    body = (
        _product_skill_body().replace(
            "Evidence supports interfaces and contracts [file: a.rs:10].",
            "Evidence supports interfaces and contracts [file: made_up.py:99].",
        )
    )
    chat = _RepairChat(["## Interfaces And Contracts\nRoutes [file: made_up.py:99].\n"] * 4)

    out = await repair_loop(
        {
            "evidence": _evi(),
            "skill_drafts": [
                SkillDraft(name="product-skill", tier="application", body=body)
            ],
        },
        chat=chat,
    )

    assert chat.prompts == []
    repaired = out["skill_drafts"][0].body
    assert "made_up.py" not in repaired
    assert "[file: a.rs:10]" in repaired
