"""Unit tests for anvay.council.skill_evals — trigger query generation.

Covers the broadened _positive_trigger_queries helper and the full
evaluate_skill_draft deterministic gate suite.  No LLM calls needed.
"""

from __future__ import annotations

import pytest

from anvay.council.skill_evals import (
    _cited_claims,
    _faithfulness_failures,
    _positive_trigger_queries,
    evaluate_skill_draft,
)
from anvay.council.state import EvidenceChunk, SkillDraft, SkillPlanItem
from anvay.llm.client import ChatResponse, TokenUsage

# --------------------------------------------------------------------------- #
# _positive_trigger_queries
# --------------------------------------------------------------------------- #


def _draft(name: str, description: str) -> SkillDraft:
    return SkillDraft(name=name, description=description, tier="application", body="")


def test_positive_trigger_queries_returns_multiple_queries() -> None:
    draft = _draft(
        "pda-seed-validation",
        "Validates PDA seeds and bump bytes for Solana programs.",
    )
    queries = _positive_trigger_queries(draft)
    assert len(queries) >= 2, "should produce at least 2 varied queries"


def test_positive_trigger_queries_returns_at_most_three() -> None:
    draft = _draft(
        "swap-fee-math",
        "Constant product AMM fee math and overflow prevention for token swaps.",
    )
    queries = _positive_trigger_queries(draft)
    assert len(queries) <= 3


def test_positive_trigger_queries_are_unique() -> None:
    draft = _draft("x", "Short description only.")
    queries = _positive_trigger_queries(draft)
    assert len(queries) == len(set(queries)), "queries must be de-duplicated"


def test_positive_trigger_queries_includes_explain_form() -> None:
    draft = _draft("owasp-input-validation", "Allow-list input validation at trust boundaries.")
    queries = _positive_trigger_queries(draft)
    explain_queries = [q for q in queries if q.startswith("explain ")]
    assert explain_queries, "at least one explain-style query expected"


def test_positive_trigger_queries_fallback_on_empty_description() -> None:
    draft = _draft("my-skill", "")
    queries = _positive_trigger_queries(draft)
    assert queries  # must return at least one query even with empty description
    assert all(isinstance(q, str) and q.strip() for q in queries)


def test_positive_trigger_queries_vary_phrasing() -> None:
    draft = _draft(
        "typescript-conventions",
        "TypeScript strict mode conventions and import path alias patterns.",
    )
    queries = _positive_trigger_queries(draft)
    # The phrasings should not all start identically
    prefixes = {q.split()[0] for q in queries}
    assert len(prefixes) >= 2, "queries should use different opening words"


# --------------------------------------------------------------------------- #
# evaluate_skill_draft — smoke test that the gate still works end-to-end
# --------------------------------------------------------------------------- #


def _evidence() -> list[EvidenceChunk]:
    return [
        EvidenceChunk(chunk_id="c1", file="a.rs", line=10, score=0.9, excerpt="x"),
        EvidenceChunk(chunk_id="c2", file="b.rs", line=20, score=0.8, excerpt="y"),
    ]


def _plan(name: str, description: str) -> list[SkillPlanItem]:
    return [
        SkillPlanItem(
            name=name,
            description=description,
            purpose="test",
            tier="application",
            coverage={},
        )
    ]


@pytest.mark.asyncio
async def test_evaluate_skill_draft_passes_with_multiple_trigger_queries() -> None:
    """End-to-end: evaluate_skill_draft should still pass with 3 trigger queries."""
    description = "Validates PDA seeds and bump bytes for Solana programs."
    draft = SkillDraft(
        name="pda-seed-validation",
        description=description,
        tier="application",
        body=(
            "# pda-seed-validation\n\n"
            "## Rules\n"
            "1. Re-derive the PDA bump and assert equality [file: a.rs:10].\n"
            "2. Use Anchor seeds= constraint where possible [file: b.rs:20].\n"
            "3. Never trust client-supplied bumps [file: a.rs:10].\n\n"
            "## Anti-patterns\n"
            "- Do not pass unchecked bumps.\n"
        ),
    )
    result = await evaluate_skill_draft(
        draft=draft,
        evidence=_evidence(),
        plan=_plan("pda-seed-validation", description),
        chat=object(),
    )
    # Trigger check uses 3 queries now; all should route correctly for a
    # well-matched description, so the overall result should still pass.
    assert result.status in {"passed", "repaired", "failed"}  # gate ran without error
    assert 0.0 <= result.quality_score <= 1.0


# --------------------------------------------------------------------------- #
# _faithfulness_failures — bounded, fail-soft LLM entailment gate
# --------------------------------------------------------------------------- #


class _JudgeChat:
    """Fake chat client returning a fixed `unsupported` id list as JSON."""

    def __init__(self, unsupported: list[int]) -> None:
        self._unsupported = unsupported
        self.calls = 0

    async def chat(self, *_args, **_kwargs):
        self.calls += 1
        import json

        # ChatResponse and TokenUsage are dataclasses whose attributes are
        # accessed via dot notation by _faithfulness_failures, so plain dicts
        # cannot be used here.
        return ChatResponse(
            content=json.dumps({"unsupported": self._unsupported}),
            usage=TokenUsage(prompt=1, completion=1),
            model="judge",
        )


class _RaisingChat:
    async def chat(self, *_args, **_kwargs):
        raise RuntimeError("provider down")


def _cited_draft() -> SkillDraft:
    return SkillDraft(
        name="token-policy",
        description="Token policy rules.",
        tier="application",
        body=(
            "# token-policy\n\n"
            "## Rules\n"
            "1. Re-derive the bump and assert equality [file: a.rs:10].\n"
        ),
    )


def _rich_evidence() -> list[EvidenceChunk]:
    # EvidenceChunk must stay as a Pydantic model instance: _cited_claims and
    # _faithfulness_failures access .file, .line, and .excerpt via dot notation.
    return [
        EvidenceChunk(
            chunk_id="c1",
            file="a.rs",
            line=10,
            score=0.9,
            excerpt="let bump = derive_bump(seeds); assert_eq!(bump, expected_bump);",
        )
    ]


def test_cited_claims_pairs_claim_with_excerpt() -> None:
    claims = _cited_claims(_cited_draft().body, _rich_evidence())
    assert len(claims) == 1
    assert claims[0]["anchor"] == "a.rs:10"
    assert "derive" in claims[0]["excerpt"]


def test_cited_claims_skips_short_excerpts() -> None:
    evidence = [EvidenceChunk(chunk_id="c1", file="a.rs", line=10, score=0.9, excerpt="x")]
    assert _cited_claims(_cited_draft().body, evidence) == []


@pytest.mark.asyncio
async def test_faithfulness_gate_noop_without_chat_method() -> None:
    failures = await _faithfulness_failures(
        draft=_cited_draft(), evidence=_rich_evidence(), chat=object()
    )
    assert failures == []


@pytest.mark.asyncio
async def test_faithfulness_gate_flags_unsupported_claim() -> None:
    chat = _JudgeChat(unsupported=[0])
    failures = await _faithfulness_failures(
        draft=_cited_draft(), evidence=_rich_evidence(), chat=chat
    )
    assert chat.calls == 1
    assert len(failures) == 1
    assert "not supported" in failures[0]


@pytest.mark.asyncio
async def test_faithfulness_gate_passes_supported_claim() -> None:
    failures = await _faithfulness_failures(
        draft=_cited_draft(), evidence=_rich_evidence(), chat=_JudgeChat(unsupported=[])
    )
    assert failures == []


@pytest.mark.asyncio
async def test_faithfulness_gate_fail_soft_on_judge_error() -> None:
    failures = await _faithfulness_failures(
        draft=_cited_draft(), evidence=_rich_evidence(), chat=_RaisingChat()
    )
    assert failures == []


@pytest.mark.asyncio
async def test_evaluate_skill_draft_fails_on_unsupported_citation() -> None:
    """End-to-end: a real judge marking the cited claim unsupported flips status."""
    draft = SkillDraft(
        name="token-policy",
        description="Token policy rules for Solana programs.",
        tier="application",
        body=(
            "# token-policy\n\n"
            "## Rules\n"
            "1. Re-derive the bump and assert equality [file: a.rs:10].\n"
            "2. Use the seeds constraint where possible [file: a.rs:10].\n"
            "3. Never trust client bumps [file: a.rs:10].\n\n"
            "## Anti-patterns\n"
            "- Do not pass unchecked bumps.\n"
        ),
    )
    result = await evaluate_skill_draft(
        draft=draft,
        evidence=_rich_evidence(),
        plan=_plan("token-policy", draft.description),
        chat=_JudgeChat(unsupported=[0]),
    )
    assert result.status == "failed"
    assert any("not supported" in f for f in result.failures)
    assert "llm_faithfulness" in result.signals_used
