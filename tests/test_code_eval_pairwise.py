"""Unit tests for the code retrieval eval — pairwise judge logic.

Tests the position-bias mitigation (A/B swap) and the new CoT-aware
_pairwise_one_order helper.  No network calls needed.
"""

from __future__ import annotations

import pytest

from evals.common import GoldenItem
from evals.run_code_eval import _matched_expected_file, _pairwise, _pairwise_one_order

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _item(*, expected: str = "good answer", anti: str = "bad answer") -> GoldenItem:
    return GoldenItem(
        id="t",
        query="q",
        expected_answer=expected,
        expected_skill="",
        expected_files=[],
        complexity="simple",
        anti_answer=anti,
    )


def _hit_with_excerpt(excerpt: str) -> object:
    class _Hit:
        pass

    h = _Hit()
    h.excerpt = excerpt  # type: ignore[attr-defined]
    return h


def test_matched_expected_file_returns_canonical_expected_path() -> None:
    relevant = {"src/auth.py", "src/billing.py"}
    assert _matched_expected_file("/repo/src/auth.py:10", relevant) == "src/auth.py"
    assert _matched_expected_file("/repo/src/other.py:10", relevant) is None


class _StubJudge:
    """Simulate a judge that always picks the *first* answer (A) — pure position bias."""

    async def chat_json(self, messages, **kwargs):
        # Always picks A regardless of content
        return {"reasoning": "A is first", "choice": "A", "rationale": "first"}, None


class _SmartJudge:
    """Simulate a judge that correctly identifies the 'good' answer by content."""

    async def chat_json(self, messages, **kwargs):
        # Reads the user message to figure out which position holds "good answer"
        user_content = messages[-1]["content"]
        choice = "A" if "ANSWER_A:\ngood answer" in user_content else "B"
        return {
            "reasoning": "good answer is more grounded",
            "choice": choice,
            "rationale": "accurate",
        }, None


class _FailingJudge:
    """Simulate a judge that always raises."""

    async def chat_json(self, messages, **kwargs):
        raise RuntimeError("network error")


# --------------------------------------------------------------------------- #
# _pairwise_one_order
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pairwise_one_order_expected_is_a_correct_pick() -> None:
    item = _item()
    judge = _SmartJudge()
    result = await _pairwise_one_order(
        item, contexts="ctx", judge=judge, expected_is_a=True
    )
    assert result is True


@pytest.mark.asyncio
async def test_pairwise_one_order_expected_is_b_correct_pick() -> None:
    item = _item()
    judge = _SmartJudge()
    result = await _pairwise_one_order(
        item, contexts="ctx", judge=judge, expected_is_a=False
    )
    # expected is B; smart judge picks B, so expected wins → True
    assert result is True


@pytest.mark.asyncio
async def test_pairwise_one_order_judge_failure_returns_none() -> None:
    item = _item()
    result = await _pairwise_one_order(
        item, contexts="ctx", judge=_FailingJudge(), expected_is_a=True
    )
    assert result is None


# --------------------------------------------------------------------------- #
# _pairwise — position-bias mitigation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pairwise_smart_judge_wins_both_orderings() -> None:
    """A judge that picks the correct answer regardless of position → True."""
    item = _item()
    hits = [_hit_with_excerpt("some context")]
    result = await _pairwise(item, hits, _SmartJudge())
    assert result is True


@pytest.mark.asyncio
async def test_pairwise_biased_judge_fails_on_swap() -> None:
    """A judge that always picks A (pure position bias) wins AB but loses BA → False.

    This is the core invariant: position-biased judges must not inflate PPA.
    """
    item = _item()
    hits = [_hit_with_excerpt("some context")]
    result = await _pairwise(item, hits, _StubJudge())
    # AB: expected=A → judge picks A → ab=True
    # BA: expected=B → judge picks A (wrong) → ba=False
    # Final: True AND False = False
    assert result is False


@pytest.mark.asyncio
async def test_pairwise_both_fail_returns_none() -> None:
    item = _item()
    result = await _pairwise(item, [], _FailingJudge())
    assert result is None


@pytest.mark.asyncio
async def test_pairwise_one_fails_returns_other() -> None:
    """If one ordering times out, we fall back to the surviving result."""

    class _PartialFailJudge:
        _calls = 0

        async def chat_json(self, messages, **kwargs):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("first call fails")
            user = messages[-1]["content"]
            choice = "A" if "ANSWER_A:\ngood answer" in user else "B"
            return {"reasoning": "ok", "choice": choice, "rationale": "ok"}, None

    item = _item()
    result = await _pairwise(item, [], _PartialFailJudge())
    # Only the second order (BA, expected=B) succeeded; smart pick → True
    assert result is True
