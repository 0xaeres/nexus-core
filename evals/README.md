# Nexus Eval Harness

Nexus evals are split by product behavior, but run through one command:

```bash
uv run nexus eval run --suite all
```

Each run writes:

- `artifacts/evals/<run_id>/summary.json`
- `artifacts/evals/<run_id>/summary.md`
- one suite JSON file, such as `retrieval.json`, `rag.json`, or `code.json`

## Suites

| Suite | Dataset | Default fixture | Product | Metrics |
|---|---|---|---|---|
| `retrieval` | `tests/eval/queries.json` | repo root (`.`) | `nexus` | `recall_at_k`, `mrr` |
| `rag` | `evals/golden.jsonl` | `evals/fixtures/skills/seed` | `forge` | `faithfulness`, `answer_relevancy`, `context_recall` |
| `code` | `evals/golden.jsonl` | `evals/fixtures/skills/seed` | `forge` | `ndcg_at_10`, `recall_at_10`, pairwise preference |

Suite defaults can be overridden:

```bash
uv run nexus eval run \
  --suite retrieval \
  --product my-product \
  --fixture /path/to/source \
  --out-dir artifacts/evals
```

Use `--no-ingest-fixture` only when the target product index is already loaded.

## CI Contract

Pull requests run the deterministic retrieval suite when `DEEPINFRA_API_KEY` is
available. Pushes to `main`, scheduled runs, and manual workflow runs also run
the LLM-judged `rag` and `code` suites with `--limit 10` for cost control.

The harness uploads JSON/Markdown artifacts so regressions can be inspected
without rerunning the job. If `evals/baseline_faithfulness.txt` exists, CI fails
when RAG faithfulness drops by more than `0.05`.

## Adding Eval Cases

Add retrieval-only cases to `tests/eval/queries.json`. Use exact expected files
and line ranges where possible; broad whole-file matches are acceptable for
architecture questions.

Add answer-quality cases to `evals/golden.jsonl`. Prefer examples from human
review rejects, approved corrections, and `report_outcome` failures. Include
`anti_answer` when a plausible wrong answer exists; the code suite uses it for
pairwise preference scoring.

## Cost Notes

`retrieval` calls embedding/rerank endpoints during ingest and query.
`rag` and `code` also use council-model judge calls. Use `--limit` for smoke
runs and full suites before retrieval/council releases.
