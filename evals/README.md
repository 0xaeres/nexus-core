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
| `retrieval` | `tests/eval/queries.json` | repo root (`.`) | `nexus` | `recall_at_k`, `mrr`, `ndcg_at_k` |
| `rag` | `evals/golden.jsonl` | `evals/fixtures/skills/seed` | `forge` | `faithfulness`, `answer_correctness`, `context_recall` |
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

### Synthetic Multi-Language Suite

To evaluate retrieval, RAG, and code search capabilities across all 10 supported programming languages (Python, TS, TSX, JS, Rust, Go, Java, C++, Kotlin, Solidity) in a unified run, use the synthetic dataset:

```bash
uv run nexus eval run \
  --suite retrieval,rag,code \
  --product synthetic \
  --fixture evals/fixtures/synthetic_project \
  --golden evals/synthetic_queries.jsonl \
  --limit 10 \
  --out-dir artifacts/evals-synthetic
```

The unified harness automatically compiles the mock multi-language source files inside `evals/fixtures/synthetic_project/` from the template before running ingestion and evaluation.

## LLM Judge Design

### RAG suite (`run_ragas.py`)

The RAG suite uses an LLM judge for two of its three metrics:

- **Faithfulness** and **answer correctness** are LLM-judged. The judge is the
  `models.evaluator` model when configured, otherwise falls back to
  `models.council`. Using a separate evaluator model avoids self-preference
  bias (the judge should not grade its own generated answers).
- **Context recall** is a heuristic: fraction of `expected_files` covered by at
  least one retrieved hit, matched by URI suffix.

Judge prompts require a structured rationale before assigning a score. The JSON schema is `{"reasoning": "...", "score": 0.0–1.0, "verdict": "..."}`.
`temperature=0` is enforced for deterministic, reproducible scores.

### Code suite (`run_code_eval.py`)

Pairwise preference accuracy (PPA) is computed for golden items that supply an
`anti_answer`. The judge runs **twice per item** — once with the expected answer
in position A, once in position B — and only counts a preference as confirmed
when the expected answer wins regardless of position. This eliminates the
systematic first-position bias that would otherwise inflate PPA scores.

The judge prompt also returns rationale JSON: `{"reasoning": "...", "choice": "A"|"B", "rationale": "..."}`.

### Inner-loop skill eval (`nexus/council/skill_evals.py`)

The council pipeline runs **5 deterministic checks** (identity, structure, name
match, citation faithfulness, trigger routing) — deliberately *not* an LLM
judge, to avoid self-grading noise. The trigger check generates **3 varied
phrasings** per skill (imperative, how-to, explain-form) to exercise routing
beyond a single lexical overlap.

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
`rag` and `code` also use LLM judge calls — `rag` uses the evaluator model (or
council fallback) for faithfulness/correctness; `code` calls it twice per pairwise
item due to position-swap mitigation. Use `--limit` for smoke runs and full
suites before retrieval/council releases.
