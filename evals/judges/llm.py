"""Shared LLM-as-judge helpers for Nexus evals."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nexus.llm.client import ChatClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JudgeScore:
    score: float
    reasoning: str
    verdict: str = ""


@dataclass(frozen=True)
class PairwiseJudgment:
    expected_preferred: bool | None
    reasoning: str
    choice: str = ""


FAITHFULNESS_PROMPT = (
    "You are a strict faithfulness grader. Given a QUESTION, an ANSWER, and the "
    "CONTEXTS the answer was supposed to draw from, evaluate whether every "
    "meaningful factual claim in the ANSWER is supported by the CONTEXTS.\n"
    "Step 1 - List each factual claim in the ANSWER.\n"
    "Step 2 - For each claim, state whether it is directly supported, partially "
    "supported, or absent from the CONTEXTS.\n"
    "Step 3 - Assign a score: 1.0 = fully grounded, 0.0 = mostly hallucinated.\n"
    'Output ONLY JSON: {"reasoning": "claim check", "score": 0.0-1.0, '
    '"verdict": "faithful" | "partial" | "hallucinated"}.'
)

ANSWER_CORRECTNESS_PROMPT = (
    "You are a strict answer-correctness grader. Given a QUESTION, an ANSWER, "
    "and an EXPECTED_ANSWER, evaluate how well the answer addresses the question "
    "and matches the expected content.\n"
    "Step 1 - Identify the core information need.\n"
    "Step 2 - Check whether the ANSWER covers it and aligns with EXPECTED_ANSWER.\n"
    "Step 3 - Assign a score: 1.0 = correct and complete, 0.0 = wrong or irrelevant.\n"
    'Output ONLY JSON: {"reasoning": "correctness check", "score": 0.0-1.0, '
    '"verdict": "correct" | "partial" | "incorrect"}.'
)

PAIRWISE_PROMPT = (
    "You are a rigorous answer-quality judge. You will be given a QUESTION, "
    "CONTEXTS retrieved to answer it, and two candidate answers: ANSWER_A and "
    "ANSWER_B. Decide which answer is more accurate and better grounded in the "
    "CONTEXTS.\n"
    "Step 1 - Identify the key facts the QUESTION requires.\n"
    "Step 2 - Check each answer against the CONTEXTS for accuracy.\n"
    "Step 3 - Pick the better-grounded answer.\n"
    'Output ONLY JSON: {"reasoning": "comparison", "choice": "A" | "B", '
    '"rationale": "1 sentence"}.'
)


def evaluator_client(config, *, role: str) -> ChatClient:
    model_cfg = config.models.evaluator or config.models.council
    return ChatClient.from_cfg(model_cfg, role=role)


async def judge_score(judge, system: str, user: str, *, max_tokens: int = 400) -> JudgeScore:
    try:
        payload, _ = await judge.chat_json(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user[:6000]},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
        )
    except Exception as e:
        log.warning("judge call failed: %s", e)
        return JudgeScore(score=0.0, reasoning=f"judge error: {e}")
    return JudgeScore(
        score=_clamp_score(payload.get("score", 0.0)),
        reasoning=str(payload.get("reasoning") or payload.get("notes", "")),
        verdict=str(payload.get("verdict") or ""),
    )


async def judge_faithfulness(
    judge,
    *,
    question: str,
    answer: str,
    contexts: list[str],
) -> JudgeScore:
    return await judge_score(
        judge,
        FAITHFULNESS_PROMPT,
        user=f"QUESTION:\n{question}\n\nANSWER:\n{answer}\n\nCONTEXTS:\n"
        + "\n---\n".join(contexts[:6]),
    )


async def judge_answer_correctness(
    judge,
    *,
    question: str,
    answer: str,
    expected_answer: str,
) -> JudgeScore:
    expected = expected_answer or "(not provided)"
    return await judge_score(
        judge,
        ANSWER_CORRECTNESS_PROMPT,
        user=f"QUESTION:\n{question}\n\nANSWER:\n{answer}\n\nEXPECTED_ANSWER:\n{expected}",
    )


async def judge_pairwise_preference(
    judge,
    *,
    question: str,
    contexts: str,
    expected_answer: str,
    anti_answer: str,
    expected_is_a: bool,
) -> PairwiseJudgment:
    answer_a = expected_answer if expected_is_a else anti_answer
    answer_b = anti_answer if expected_is_a else expected_answer
    user = (
        f"QUESTION:\n{question}\n\n"
        f"CONTEXTS:\n{contexts}\n\n"
        f"ANSWER_A:\n{answer_a}\n\n"
        f"ANSWER_B:\n{answer_b}\n"
    )
    try:
        payload, _ = await judge.chat_json(
            [
                {"role": "system", "content": PAIRWISE_PROMPT},
                {"role": "user", "content": user[:6000]},
            ],
            temperature=0.0,
            max_tokens=250,
        )
    except Exception as e:
        log.warning("pairwise judge failed (expected_is_a=%s): %s", expected_is_a, e)
        return PairwiseJudgment(expected_preferred=None, reasoning=f"judge error: {e}")
    choice = str(payload.get("choice", "")).strip().upper()
    expected_choice = "A" if expected_is_a else "B"
    return PairwiseJudgment(
        expected_preferred=choice == expected_choice,
        reasoning=str(payload.get("reasoning") or payload.get("rationale") or ""),
        choice=choice,
    )


def _clamp_score(value: object) -> float:
    try:
        score = float(value or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return max(0.0, min(1.0, score))
