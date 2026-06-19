"""Unified Nexus eval harness.

Runs retrieval, RAG answer, and code retrieval eval suites with optional fixture
ingest and machine-readable artifacts. Existing suite runners stay standalone;
this module gives CI and humans one stable entrypoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from evals.run_code_eval import CodeReport
from evals.run_code_eval import run as run_code_eval
from evals.run_ragas import Report as RagReport
from evals.run_ragas import run as run_rag_eval
from nexus.config import NexusConfig
from nexus.connectors.local_fs import LocalFsConfig, LocalFsSource
from nexus.ingest.pipeline import IngestStats, run_ingest
from tests.eval.harness import EvalReport as RetrievalReport
from tests.eval.harness import load_queries
from tests.eval.harness import run_eval as run_retrieval_eval

log = logging.getLogger(__name__)

SuiteName = Literal["retrieval", "rag", "code"]
ALL_SUITES: tuple[SuiteName, ...] = ("retrieval", "rag", "code")
DEFAULT_OUT_DIR = Path("artifacts/evals")
DEFAULT_GOLDEN = Path("evals/golden.jsonl")
DEFAULT_FORGE_FIXTURE = Path("evals/fixtures/skills/seed")
DEFAULT_RETRIEVAL_FIXTURE = Path(".")


@dataclass(frozen=True)
class SuiteDefaults:
    product_id: str
    fixture_path: Path


@dataclass
class SuiteArtifact:
    suite: SuiteName
    passed: bool
    product_id: str
    output_json: str
    metrics: dict[str, float]
    thresholds: dict[str, float]
    ingest: dict | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class EvalRunArtifact:
    run_id: str
    generated_at: str
    config_path: str
    config_fingerprint: dict
    suites: list[SuiteArtifact]
    output_dir: str

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.suites)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["passed"] = self.passed
        return data


def parse_suites(value: str) -> tuple[SuiteName, ...]:
    raw = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not raw or raw == ["all"]:
        return ALL_SUITES
    invalid = [part for part in raw if part not in ALL_SUITES]
    if invalid:
        raise ValueError(f"unknown eval suite(s): {', '.join(invalid)}")
    return tuple(dict.fromkeys(raw))  # stable de-dupe


def suite_defaults(suite: SuiteName) -> SuiteDefaults:
    if suite == "retrieval":
        meta, _ = load_queries()
        return SuiteDefaults(
            product_id=str(meta.get("ingested_product_id") or "nexus"),
            fixture_path=DEFAULT_RETRIEVAL_FIXTURE,
        )
    return SuiteDefaults(product_id="forge", fixture_path=DEFAULT_FORGE_FIXTURE)


async def run_suites(
    *,
    suites: tuple[SuiteName, ...],
    config: NexusConfig,
    config_path: Path,
    out_dir: Path = DEFAULT_OUT_DIR,
    product_id: str | None = None,
    fixture_path: Path | None = None,
    ingest_fixture: bool = True,
    golden_path: Path = DEFAULT_GOLDEN,
    limit: int | None = None,
    top_k: int = 10,
) -> EvalRunArtifact:
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_dir = out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[SuiteArtifact] = []
    for suite in suites:
        defaults = suite_defaults(suite)
        suite_product = product_id or defaults.product_id
        suite_fixture = fixture_path or defaults.fixture_path
        ingest: dict | None = None
        if ingest_fixture:
            ingest = _ingest_stats_to_dict(
                await _ingest_fixture(
                    product_id=suite_product,
                    fixture_path=suite_fixture,
                    config=config,
                )
            )

        if suite == "retrieval":
            artifacts.append(
                await _run_retrieval_suite(
                    config=config,
                    product_id=suite_product,
                    out_dir=run_dir,
                    ingest=ingest,
                    top_k=top_k,
                )
            )
        elif suite == "rag":
            artifacts.append(
                await _run_rag_suite(
                    config=config,
                    product_id=suite_product,
                    out_dir=run_dir,
                    ingest=ingest,
                    golden_path=golden_path,
                    limit=limit,
                )
            )
        elif suite == "code":
            artifacts.append(
                await _run_code_suite(
                    config=config,
                    product_id=suite_product,
                    out_dir=run_dir,
                    ingest=ingest,
                    golden_path=golden_path,
                    limit=limit,
                )
            )

    artifact = EvalRunArtifact(
        run_id=run_id,
        generated_at=datetime.now(UTC).isoformat(),
        config_path=str(config_path),
        config_fingerprint=_config_fingerprint(config),
        suites=artifacts,
        output_dir=str(run_dir),
    )
    (run_dir / "summary.json").write_text(
        json.dumps(artifact.as_dict(), indent=2), encoding="utf-8"
    )
    (run_dir / "summary.md").write_text(render_markdown_summary(artifact), encoding="utf-8")
    return artifact


async def _ingest_fixture(
    *,
    product_id: str,
    fixture_path: Path,
    config: NexusConfig,
) -> IngestStats:
    path = fixture_path.resolve()
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError(f"eval fixture not found: {path}")
    log.info("ingesting eval fixture %s into product %s", path, product_id)
    source = LocalFsSource(LocalFsConfig(root=path))
    return await run_ingest(product_id=product_id, source=source, config=config, enrich=False)


async def _run_retrieval_suite(
    *,
    config: NexusConfig,
    product_id: str,
    out_dir: Path,
    ingest: dict | None,
    top_k: int,
) -> SuiteArtifact:
    meta, queries = load_queries()
    report = await run_retrieval_eval(
        config=config, product_id=product_id, top_k=top_k, queries=queries
    )
    output = out_dir / "retrieval.json"
    payload = {
        "suite": "retrieval",
        "product_id": product_id,
        "metrics": {"recall_at_k": report.recall_at_k, "mrr": report.mrr},
        "thresholds": {
            "min_recall_at_10": float(meta.get("min_recall_at_10", 0.0)),
            "min_mrr": float(meta.get("min_mrr", 0.0)),
        },
        "misses": _retrieval_misses(report),
        "items": [
            {
                "query": result.query,
                "first_match_rank": result.first_match_rank,
                "tags": result.tags,
                "top_files": _top_files(result.top_k_hits),
            }
            for result in report.results
        ],
    }
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    thresholds = payload["thresholds"]
    passed = (
        report.recall_at_k >= thresholds["min_recall_at_10"]
        and report.mrr >= thresholds["min_mrr"]
    )
    return SuiteArtifact(
        suite="retrieval",
        passed=passed,
        product_id=product_id,
        output_json=str(output),
        metrics=payload["metrics"],
        thresholds=thresholds,
        ingest=ingest,
        notes=[f"{len(payload['misses'])} misses"],
    )


async def _run_rag_suite(
    *,
    config: NexusConfig,
    product_id: str,
    out_dir: Path,
    ingest: dict | None,
    golden_path: Path,
    limit: int | None,
) -> SuiteArtifact:
    output = out_dir / "rag.json"
    report: RagReport = await run_rag_eval(
        golden_path=golden_path,
        product_id=product_id,
        config=config,
        output=output,
        limit=limit,
    )
    return SuiteArtifact(
        suite="rag",
        passed=report.passed(),
        product_id=product_id,
        output_json=str(output),
        metrics=report.aggregates,
        thresholds=asdict(report.thresholds),
        ingest=ingest,
    )


async def _run_code_suite(
    *,
    config: NexusConfig,
    product_id: str,
    out_dir: Path,
    ingest: dict | None,
    golden_path: Path,
    limit: int | None,
) -> SuiteArtifact:
    output = out_dir / "code.json"
    report: CodeReport = await run_code_eval(
        golden_path=golden_path,
        product_id=product_id,
        config=config,
        output=output,
        limit=limit,
    )
    return SuiteArtifact(
        suite="code",
        passed=report.passed(),
        product_id=product_id,
        output_json=str(output),
        metrics=report.aggregates,
        thresholds=asdict(report.thresholds),
        ingest=ingest,
    )


def render_markdown_summary(artifact: EvalRunArtifact) -> str:
    lines = [
        f"# Nexus Eval Run {artifact.run_id}",
        "",
        f"- Status: {'PASS' if artifact.passed else 'FAIL'}",
        f"- Generated: {artifact.generated_at}",
        f"- Config: `{artifact.config_path}`",
        f"- Output: `{artifact.output_dir}`",
        "",
        "| Suite | Product | Status | Metrics | Output |",
        "|---|---|---|---|---|",
    ]
    for suite in artifact.suites:
        metrics = ", ".join(f"{key}={value:.4g}" for key, value in suite.metrics.items())
        lines.append(
            f"| {suite.suite} | `{suite.product_id}` | "
            f"{'PASS' if suite.passed else 'FAIL'} | {metrics} | `{suite.output_json}` |"
        )
    return "\n".join(lines) + "\n"


def _retrieval_misses(report: RetrievalReport) -> list[str]:
    return [result.query for result in report.results if not result.hit]


def _top_files(hits) -> list[str]:
    files: list[str] = []
    for hit in hits[:5]:
        uri = str((hit.payload or {}).get("resource_uri") or "")
        if uri and uri not in files:
            files.append(uri)
    return files


def _ingest_stats_to_dict(stats: IngestStats) -> dict:
    return {
        "resources_seen": stats.resources_seen,
        "resources_indexed": stats.resources_indexed,
        "resources_skipped": stats.resources_skipped,
        "resources_failed": stats.resources_failed,
        "chunks_produced": stats.chunks_produced,
        "chunks_indexed": stats.chunks_indexed,
        "graph_resources_indexed": stats.graph_resources_indexed,
    }


def _config_fingerprint(config: NexusConfig) -> dict:
    return {
        "embedding_provider": config.models.embedding.provider,
        "embedding_model": config.models.embedding.model,
        "embedding_dim": config.models.embedding.dim,
        "embedding_profile": config.models.embedding.instruction_profile,
        "reranker_provider": config.models.reranker.provider,
        "reranker_model": config.models.reranker.model,
        "council_provider": config.models.council.provider,
        "council_model": config.models.council.model,
        "qdrant_url": config.vector_store.url,
        "qdrant_collections": config.vector_store.collections.model_dump(),
        "quantization": config.vector_store.quantization.model_dump(),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run Nexus eval suites.")
    parser.add_argument("--suite", default="all", help="all, retrieval, rag, code, or comma list")
    parser.add_argument("--config", type=Path, default=Path("nexus.yaml"))
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--product", default=None, help="Override suite default product id")
    parser.add_argument("--fixture", type=Path, default=None, help="Override suite default fixture")
    parser.add_argument("--no-ingest-fixture", action="store_true")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--limit", type=int, default=None, help="Limit RAG/code judge items")
    parser.add_argument("--top-k", type=int, default=10, help="Retrieval top-k")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
    config = NexusConfig.load(args.config)
    artifact = asyncio.run(
        run_suites(
            suites=parse_suites(args.suite),
            config=config,
            config_path=args.config,
            out_dir=args.out_dir,
            product_id=args.product,
            fixture_path=args.fixture,
            ingest_fixture=not args.no_ingest_fixture,
            golden_path=args.golden,
            limit=args.limit,
            top_k=args.top_k,
        )
    )
    print(render_markdown_summary(artifact))
    return 0 if artifact.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
