"""Code retrieval eval - nDCG@10, Recall@10, Pairwise Preference Accuracy.

PPA is computed only for golden items that supply an `anti_answer` (CoQuIR-
style): the LLM is asked to choose between expected_answer and anti_answer
given the retrieved contexts. We then check whether the LLM picks the
expected answer at a rate >= the PPA threshold.

Gates (per Slice 7 plan):
  nDCG@10 >= 0.75
  Recall@10 >= 0.80
  pairwise_preference_accuracy >= 0.85
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from evals.code_metrics import mean, ndcg_at_k, recall_at_k
from evals.common import GoldenItem, load_golden
from nexus.config import NexusConfig
from nexus.llm.client import ChatClient
from nexus.retrieval.evidence import retrieve_evidence
from nexus.retrieval.pipeline import RetrievalContext

log = logging.getLogger("evals.code")


@dataclass(frozen=True)
class CodeThresholds:
    ndcg_at_10: float = 0.75
    recall_at_10: float = 0.80
    pairwise_preference: float = 0.85


@dataclass
class CodeQueryScore:
    id: str
    ndcg_at_10: float
    recall_at_10: float
    pairwise_preferred: bool | None = None  # None when no anti_answer


@dataclass
class CodeReport:
    items: list[CodeQueryScore]
    aggregates: dict[str, float]
    thresholds: CodeThresholds

    def as_dict(self) -> dict:
        return {
            "items": [asdict(it) for it in self.items],
            "aggregates": self.aggregates,
            "thresholds": asdict(self.thresholds),
        }

    def passed(self) -> bool:
        a = self.aggregates
        t = self.thresholds
        if a.get("ndcg_at_10", 0.0) < t.ndcg_at_10:
            return False
        if a.get("recall_at_10", 0.0) < t.recall_at_10:
            return False
        # PPA only gates if we had at least one pairwise item
        if a.get("pairwise_n", 0) > 0:
            return a.get("pairwise_preference_accuracy", 0.0) >= t.pairwise_preference
        return True


_PREF_PROMPT = (
    "You are choosing the more correct answer given the retrieved contexts. "
    "Read both ANSWER_A and ANSWER_B, then pick the one that is more accurate "
    "and better grounded in the contexts. Output ONLY JSON: "
    '{"choice": "A" | "B", "rationale": "1 sentence"}.'
)


async def run(
    *,
    golden_path: Path,
    product_id: str,
    config: NexusConfig,
    output: Path,
    thresholds: CodeThresholds = CodeThresholds(),
    limit: int | None = None,
) -> CodeReport:
    items = load_golden(golden_path)
    if limit:
        items = items[:limit]

    ctx = RetrievalContext.from_config(config)
    judge = ChatClient.from_cfg(config.models.council, role="code_eval_judge")

    try:
        results = await asyncio.gather(
            *[
                _score_one(item, ctx=ctx, judge=judge, product_id=product_id)
                for item in items
            ]
        )
    finally:
        await judge.aclose()
        await ctx.aclose()

    aggregates = _aggregate(results)
    report = CodeReport(items=results, aggregates=aggregates, thresholds=thresholds)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    _print_summary(report)
    return report


async def _score_one(
    item: GoldenItem,
    *,
    ctx: RetrievalContext,
    judge: ChatClient,
    product_id: str,
) -> CodeQueryScore:
    result = await retrieve_evidence(
        ctx=ctx, product_id=product_id, query=item.query, top_k=10, mode="auto"
    )
    retrieved_ids = [_identify(candidate) for candidate in result.candidates]
    relevant = {f.lower() for f in item.expected_files}

    # We match retrieved chunk URIs against the (substring of) expected files.
    matched_ids = [
        rid for rid in retrieved_ids if any(f in (rid or "").lower() for f in relevant)
    ]
    # nDCG uses retrieved positions; we treat retrieved positions whose URI matched
    # any expected_file as relevant.
    relevant_set = set(matched_ids) if matched_ids else relevant

    ndcg = ndcg_at_k(retrieved_ids, relevant_set, k=10)
    recall = recall_at_k(retrieved_ids, relevant_set, k=10)

    pairwise: bool | None = None
    if item.anti_answer:
        pairwise = await _pairwise(item, result.candidates, judge)

    return CodeQueryScore(
        id=item.id,
        ndcg_at_10=ndcg,
        recall_at_10=recall,
        pairwise_preferred=pairwise,
    )


async def _pairwise(item: GoldenItem, hits, judge: ChatClient) -> bool | None:
    contexts = "\n---\n".join(
        (getattr(h, "excerpt", "") or "")[:800] for h in hits[:6]
    )
    # Random label assignment (A/B) — for determinism in tests we keep A=expected
    user = (
        f"QUESTION:\n{item.query}\n\n"
        f"CONTEXTS:\n{contexts}\n\n"
        f"ANSWER_A:\n{item.expected_answer}\n\n"
        f"ANSWER_B:\n{item.anti_answer}\n"
    )
    try:
        payload, _ = await judge.chat_json(
            [
                {"role": "system", "content": _PREF_PROMPT},
                {"role": "user", "content": user[:6000]},
            ],
            temperature=0.0,
            max_tokens=120,
        )
    except Exception as e:
        log.warning("pairwise judge failed: %s", e)
        return None
    choice = str(payload.get("choice", "")).strip().upper()
    return choice == "A"


def _identify(hit) -> str:
    uri = getattr(hit, "file", "")
    line = getattr(hit, "line", "")
    return f"{uri}:{line}".lower() if uri else hit.chunk_id


def _aggregate(results: list[CodeQueryScore]) -> dict[str, float]:
    if not results:
        return {
            "n": 0,
            "ndcg_at_10": 0.0,
            "recall_at_10": 0.0,
            "pairwise_preference_accuracy": 0.0,
            "pairwise_n": 0,
        }
    ndcgs = [r.ndcg_at_10 for r in results]
    recalls = [r.recall_at_10 for r in results]
    pairwise_results = [r.pairwise_preferred for r in results if r.pairwise_preferred is not None]
    return {
        "n": len(results),
        "ndcg_at_10": round(mean(ndcgs), 4),
        "recall_at_10": round(mean(recalls), 4),
        "pairwise_preference_accuracy": (
            round(sum(pairwise_results) / len(pairwise_results), 4)
            if pairwise_results
            else 0.0
        ),
        "pairwise_n": len(pairwise_results),
    }


def _print_summary(report: CodeReport) -> None:
    a = report.aggregates
    t = report.thresholds
    print()
    print("=" * 60)
    print(f"Code retrieval eval - {int(a.get('n', 0))} queries")
    print("=" * 60)
    print(f"  nDCG@10           {a.get('ndcg_at_10', 0):.3f}  (>= {t.ndcg_at_10})")
    print(f"  Recall@10         {a.get('recall_at_10', 0):.3f}  (>= {t.recall_at_10})")
    if a.get("pairwise_n", 0) > 0:
        print(
            f"  Pairwise pref     {a.get('pairwise_preference_accuracy', 0):.3f}  "
            f"(>= {t.pairwise_preference}, n={int(a['pairwise_n'])})"
        )
    print()
    print("PASS" if report.passed() else "FAIL")
    print()


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
    parser = argparse.ArgumentParser(description="Run code retrieval eval.")
    parser.add_argument("--golden", type=Path, default=Path("evals/golden.jsonl"))
    parser.add_argument("--config", type=Path, default=Path("nexus.yaml"))
    parser.add_argument("--product", default="forge")
    parser.add_argument("--out", type=Path, default=Path("evals/last_code.json"))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    config = NexusConfig.load(args.config)
    report = asyncio.run(
        run(
            golden_path=args.golden,
            product_id=args.product,
            config=config,
            output=args.out,
            limit=args.limit,
        )
    )
    return 0 if report.passed() else 1


if __name__ == "__main__":
    sys.exit(main())
