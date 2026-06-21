"""RAGAS-style eval runner.

Three scores per query, plus aggregates:

- **faithfulness** - LLM judges whether every claim in the synthesized answer
  is grounded in retrieved contexts.
- **answer_correctness** - LLM judges whether the answer matches expected_answer.
- **context_recall** - fraction of expected_files covered by at least one
  retrieved hit, matched by URI suffix (mirrors tests/eval/harness.matches_expected).

Gates (per Slice 7 plan):
  faithfulness    >= 0.85
  answer_correctness >= 0.80
  context_recall   >= 0.75

We don't actually `import ragas` - the ragas package is heavy and its prompts
churn between versions. A small in-house judge over our own LLM client is more
deterministic and easier to debug.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from evals.common import GoldenItem, load_golden
from evals.judges.llm import (
    evaluator_client,
    judge_answer_correctness,
    judge_faithfulness,
    judge_score,
)
from evals.metrics import mean
from nexus.config import NexusConfig
from nexus.llm.client import ChatClient
from nexus.retrieval.pipeline import RetrievalContext, retrieve

log = logging.getLogger("evals.ragas")


# ---------------------------------------------------------------- thresholds


@dataclass(frozen=True)
class Thresholds:
    faithfulness: float = 0.85
    answer_correctness: float = 0.80
    context_recall: float = 0.75


# ---------------------------------------------------------------- per-query


@dataclass
class QueryScore:
    id: str
    faithfulness: float
    answer_correctness: float
    context_recall: float
    answer: str = ""
    notes: str = ""


@dataclass
class Report:
    items: list[QueryScore]
    aggregates: dict[str, float]
    thresholds: Thresholds

    def as_dict(self) -> dict:
        return {
            "items": [asdict(it) for it in self.items],
            "aggregates": self.aggregates,
            "thresholds": asdict(self.thresholds),
        }

    def passed(self) -> bool:
        a = self.aggregates
        t = self.thresholds
        return (
            a.get("faithfulness", 0.0) >= t.faithfulness
            and a.get("answer_correctness", 0.0) >= t.answer_correctness
            and a.get("context_recall", 0.0) >= t.context_recall
        )


_SYNTH_PROMPT = (
    "You are answering on behalf of a code-search assistant. You have ONLY the "
    "provided CONTEXTS. Answer the QUESTION concisely (2-4 sentences). Never "
    "introduce facts not in the contexts. If the contexts are silent, say so."
)


# ---------------------------------------------------------------- runner


async def run(
    *,
    golden_path: Path,
    product_id: str,
    config: NexusConfig,
    output: Path,
    thresholds: Thresholds = Thresholds(),
    limit: int | None = None,
) -> Report:
    items = load_golden(golden_path)
    if limit:
        items = items[:limit]

    ctx = RetrievalContext.from_config(config)
    judge = evaluator_client(config, role="ragas_judge")
    answerer = ChatClient.from_cfg(config.models.council, role="ragas_answerer")

    try:
        results = await asyncio.gather(
            *[
                _score_one(item, ctx=ctx, judge=judge, answerer=answerer, product_id=product_id)
                for item in items
            ]
        )
    finally:
        await judge.aclose()
        await answerer.aclose()
        await ctx.aclose()

    aggregates = _aggregate(results)
    report = Report(items=results, aggregates=aggregates, thresholds=thresholds)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    _print_summary(report)
    return report


async def _score_one(
    item: GoldenItem,
    *,
    ctx: RetrievalContext,
    judge: ChatClient,
    answerer: ChatClient,
    product_id: str,
) -> QueryScore:
    # 1. Retrieve contexts
    result = await retrieve(
        ctx=ctx, product_id=product_id, query=item.query, top_k=8, mode="auto"
    )
    contexts = [
        (h.payload or {}).get("content", "")
        for h in result.hits
        if (h.payload or {}).get("content")
    ]

    if not contexts:
        return QueryScore(
            id=item.id,
            faithfulness=0.0,
            answer_correctness=0.0,
            context_recall=0.0,
            answer="",
            notes="no contexts retrieved",
        )

    # 2. Context recall (heuristic): expected file substrings appear in retrieved chunks
    context_recall = _heuristic_context_recall(item, result.hits)

    # 3. Synthesize an answer from contexts
    answer = await _synthesize(answerer, item.query, contexts)

    # 4. LLM-judge faithfulness + correctness in parallel
    faithfulness, correctness = await asyncio.gather(
        judge_faithfulness(
            judge,
            question=item.query,
            answer=answer,
            contexts=contexts,
        ),
        judge_answer_correctness(
            judge,
            question=item.query,
            answer=answer,
            expected_answer=item.expected_answer,
        ),
    )

    return QueryScore(
        id=item.id,
        faithfulness=faithfulness.score,
        answer_correctness=correctness.score,
        context_recall=context_recall,
        answer=answer,
        notes=f"{faithfulness.reasoning} | {correctness.reasoning}",
    )


async def _synthesize(answerer: ChatClient, question: str, contexts: list[str]) -> str:
    msg = [
        {"role": "system", "content": _SYNTH_PROMPT},
        {
            "role": "user",
            "content": (
                f"QUESTION:\n{question}\n\nCONTEXTS:\n"
                + "\n---\n".join(c[:1000] for c in contexts[:6])
            ),
        },
    ]
    resp = await answerer.chat(msg, temperature=0.2, max_tokens=400)
    return resp.content.strip()


async def _llm_judge(
    judge: ChatClient, system: str, user: str
) -> tuple[float, str]:
    result = await judge_score(judge, system, user)
    return result.score, result.reasoning


def _heuristic_context_recall(item: GoldenItem, hits) -> float:
    """Fraction of expected_files covered by at least one retrieved hit.

    Uses URI suffix matching (``resource_uri.endswith(expected_file)``) to
    avoid false positives from plain substring search.  Mirrors the logic in
    ``tests.eval.harness.matches_expected`` so both harnesses agree on what
    counts as a hit.
    """
    if not item.expected_files:
        return 1.0
    # Collect unique URIs from the retrieved hits once.
    retrieved_uris = [
        str((h.payload or {}).get("resource_uri") or "").lower()
        for h in hits
    ]
    matched = sum(
        1
        for expected_file in item.expected_files
        if _any_uri_covers(expected_file.lower(), retrieved_uris)
    )
    return matched / len(item.expected_files)


def _any_uri_covers(expected_file: str, retrieved_uris: list[str]) -> bool:
    """Return True if any retrieved URI ends with the expected file path."""
    return any(uri.endswith(expected_file) for uri in retrieved_uris if uri)


def _aggregate(results: list[QueryScore]) -> dict[str, float]:
    if not results:
        return {
            "n": 0,
            "faithfulness": 0.0,
            "answer_correctness": 0.0,
            "context_recall": 0.0,
        }
    n = len(results)
    return {
        "n": n,
        "faithfulness": round(mean([r.faithfulness for r in results]), 4),
        "answer_correctness": round(mean([r.answer_correctness for r in results]), 4),
        "context_recall": round(mean([r.context_recall for r in results]), 4),
    }


def _print_summary(report: Report) -> None:
    a = report.aggregates
    t = report.thresholds
    print()
    print("=" * 60)
    print(f"RAGAS-style eval - {int(a.get('n', 0))} queries")
    print("=" * 60)
    print(f"  faithfulness      {a.get('faithfulness', 0):.3f}  (>= {t.faithfulness})")
    print(
        f"  answer_correct    {a.get('answer_correctness', 0):.3f}  "
        f"(>= {t.answer_correctness})"
    )
    print(f"  context_recall    {a.get('context_recall', 0):.3f}  (>= {t.context_recall})")
    print()
    print("PASS" if report.passed() else "FAIL")
    print()


# ---------------------------------------------------------------- entry point


def _make_runner() -> Callable[[], Awaitable[Report]]:
    parser = argparse.ArgumentParser(description="Run RAGAS-style eval.")
    parser.add_argument("--golden", type=Path, default=Path("evals/golden.jsonl"))
    parser.add_argument("--config", type=Path, default=Path("nexus.yaml"))
    parser.add_argument("--product", default="forge")
    parser.add_argument("--out", type=Path, default=Path("evals/last_ragas.json"))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    config = NexusConfig.load(args.config)

    async def _go() -> Report:
        return await run(
            golden_path=args.golden,
            product_id=args.product,
            config=config,
            output=args.out,
            limit=args.limit,
        )

    return _go


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
    report = asyncio.run(_make_runner()())
    return 0 if report.passed() else 1


if __name__ == "__main__":
    sys.exit(main())
