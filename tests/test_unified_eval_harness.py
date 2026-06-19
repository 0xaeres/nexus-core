from __future__ import annotations

from evals.harness import (
    EvalRunArtifact,
    SuiteArtifact,
    parse_suites,
    render_markdown_summary,
    suite_defaults,
)


def test_parse_suites_all_and_dedupe() -> None:
    assert parse_suites("all") == ("retrieval", "rag", "code")
    assert parse_suites("retrieval,rag,retrieval") == ("retrieval", "rag")


def test_parse_suites_rejects_unknown() -> None:
    try:
        parse_suites("retrieval,banana")
    except ValueError as e:
        assert "banana" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_suite_defaults_are_product_specific() -> None:
    assert suite_defaults("retrieval").product_id == "nexus"
    assert suite_defaults("rag").product_id == "forge"
    assert suite_defaults("code").fixture_path.parts[-2:] == ("skills", "seed")


def test_markdown_summary_includes_suite_rows() -> None:
    artifact = EvalRunArtifact(
        run_id="r1",
        generated_at="2026-01-01T00:00:00Z",
        config_path="nexus.yaml",
        config_fingerprint={},
        output_dir="artifacts/evals/r1",
        suites=[
            SuiteArtifact(
                suite="retrieval",
                passed=True,
                product_id="nexus",
                output_json="artifacts/evals/r1/retrieval.json",
                metrics={"recall_at_k": 1.0, "mrr": 0.5},
                thresholds={"min_recall_at_10": 0.6, "min_mrr": 0.35},
            )
        ],
    )

    out = render_markdown_summary(artifact)

    assert "Status: PASS" in out
    assert "| retrieval | `nexus` | PASS |" in out
