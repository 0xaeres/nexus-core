"""Offline retrieval-quality eval against a hand-curated Q/A set.

Loads `tests/eval/queries.json`, runs each query through the full retrieval
pipeline (dense + BM25 + RRF + rerank), and reports:

- **recall@K**: fraction of queries with ≥1 expected anchor in the top-K hits
- **MRR**: mean reciprocal rank of the first matching hit per query

Designed to be invoked from a pytest test (`test_retrieval_quality.py`) or
as a standalone script (`python -m tests.eval.harness`). Requires Qdrant +
embedder + reranker to be reachable; the caller decides what to do when
they aren't (the pytest wrapper skips; the CLI exits non-zero).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from nexus.config import NexusConfig
from nexus.retrieval.evidence import EvidenceCandidate, retrieve_evidence
from nexus.retrieval.hybrid import Hit
from nexus.retrieval.pipeline import RetrievalContext

log = logging.getLogger(__name__)

QUERIES_PATH = Path(__file__).parent / "queries.json"


@dataclass
class QueryResult:
    query: str
    top_k_hits: list[Hit]
    first_match_rank: int | None  # 1-indexed; None if no match in top_k
    tags: list[str]

    @property
    def hit(self) -> bool:
        return self.first_match_rank is not None


@dataclass
class EvalReport:
    results: list[QueryResult]
    top_k: int

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def recall_at_k(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.hit) / self.total

    @property
    def mrr(self) -> float:
        if not self.results:
            return 0.0
        return sum(
            (1.0 / r.first_match_rank) if r.first_match_rank else 0.0
            for r in self.results
        ) / self.total

    def render(self) -> str:
        """Tabular text summary for the CLI / pytest output."""
        lines = [
            f"queries:       {self.total}",
            f"top_k:         {self.top_k}",
            f"recall@{self.top_k}:    {self.recall_at_k:.3f}",
            f"MRR:           {self.mrr:.3f}",
            "",
            "misses:",
        ]
        misses = [r for r in self.results if not r.hit]
        if not misses:
            lines.append("  (none — everything landed in top-K)")
        else:
            for r in misses:
                lines.append(f"  - {r.query}")
        return "\n".join(lines)


def load_queries(path: Path = QUERIES_PATH) -> tuple[dict, list[dict]]:
    """Returns (meta, queries)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("_meta", {}), data.get("queries", [])


def matches_expected(hit: Hit, expected: list[dict]) -> bool:
    """A hit matches if its resource_uri ends with one of the expected file
    paths. Line-range matching is optional: if `line_start`/`line_end` are
    provided in the expected entry, we require overlap; otherwise any chunk
    from the file counts.
    """
    payload = hit.payload or {}
    uri = str(payload.get("resource_uri") or "")
    if not uri:
        return False
    hit_start = int(payload.get("start_line") or 0)
    hit_end = int(payload.get("end_line") or hit_start)
    for ex in expected:
        path = ex.get("file", "")
        if not path or not uri.endswith(path):
            continue
        ls = ex.get("line_start")
        le = ex.get("line_end")
        if ls is None and le is None:
            return True
        ex_start = int(ls or 0)
        ex_end = int(le or ex_start)
        if hit_start <= ex_end and hit_end >= ex_start:
            return True
    return False


async def run_eval(
    *,
    config: NexusConfig,
    product_id: str,
    top_k: int = 10,
    queries: list[dict] | None = None,
) -> EvalReport:
    """Drive the retrieval pipeline over the loaded queries and score them."""
    if queries is None:
        _, queries = load_queries()

    ctx = RetrievalContext.from_config(config)
    try:
        results: list[QueryResult] = []
        for q in queries:
            text = q["query"]
            expected = q.get("expected") or []
            tags = q.get("tags") or []
            try:
                rr = await retrieve_evidence(
                    ctx=ctx, product_id=product_id, query=text, top_k=top_k, mode="auto"
                )
                hits = [_candidate_to_hit(candidate) for candidate in rr.candidates]
            except Exception as e:
                log.warning("retrieve failed for %r: %s", text, e)
                hits = []
            first_rank: int | None = None
            for i, hit in enumerate(hits, start=1):
                if matches_expected(hit, expected):
                    first_rank = i
                    break
            results.append(
                QueryResult(
                    query=text,
                    top_k_hits=hits,
                    first_match_rank=first_rank,
                    tags=tags,
                )
            )
        return EvalReport(results=results, top_k=top_k)
    finally:
        await ctx.aclose()


def _candidate_to_hit(candidate: EvidenceCandidate) -> Hit:
    return Hit(
        id=candidate.chunk_id,
        score=candidate.score,
        source=candidate.channel,
        payload={
            "resource_uri": candidate.file,
            "start_line": candidate.line,
            "end_line": candidate.end_line or candidate.line,
            "content": candidate.excerpt,
            "context_path": candidate.context_path,
            "graph_node_ids": candidate.graph_node_ids,
        },
    )


# ---------------------------------------------------------------- CLI


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Nexus retrieval eval set.")
    parser.add_argument("--product", required=True, help="product_id whose index to query")
    parser.add_argument("--config", default="nexus.yaml", help="path to nexus.yaml")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--queries",
        default=str(QUERIES_PATH),
        help="path to queries.json (defaults to bundled set)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    cfg = NexusConfig.load(args.config)
    meta, queries = load_queries(Path(args.queries))

    report = asyncio.run(
        run_eval(config=cfg, product_id=args.product, top_k=args.top_k, queries=queries)
    )
    print(report.render())

    floor_recall = float(meta.get("min_recall_at_10", 0.0))
    floor_mrr = float(meta.get("min_mrr", 0.0))
    if report.recall_at_k < floor_recall or report.mrr < floor_mrr:
        print(
            f"\nFAILED — recall@{args.top_k}={report.recall_at_k:.3f} "
            f"(min {floor_recall}), MRR={report.mrr:.3f} (min {floor_mrr})"
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
