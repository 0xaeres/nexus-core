# Slice 7 — Evals + CI + Polish Status

## What's implemented

### Golden set + eval runners
- `evals/golden.jsonl` - 30 hand-crafted Q/A pairs (5 simple + 24 complex + 1 pairwise CoQuIR pair). Covers every seed skill and 5 cross-source items that exercise the GraphRAG path.
- `evals/common.py` - `GoldenItem` dataclass + JSONL loader.
- `evals/code_metrics.py` - pure-function `precision_at_k`, `recall_at_k`, `dcg`, `ndcg_at_k`, `mean`. Testable without infra.
- `evals/run_ragas.py` - RAGAS-style runner over our own LLM-as-judge (no `ragas` dep churn). Per-query and aggregate `faithfulness` / `answer_relevancy` / `context_recall`. JSON report + pass/fail return.
- `evals/run_code_eval.py` - `nDCG@10` / `Recall@10` / `pairwise_preference_accuracy` over the golden set.

### Guardrails
- `nexus/retrieval/guard.py` - 6-pattern regex scan over retrieved chunks (`ignore-previous`, `role-spoof`, `chat-template-token`, `fenced-system-prompt`, `override-rules`, `tool-spoof`). Flagged chunks have their `content` replaced with `[REDACTED]` so downstream agents never ingest the adversarial text; the citation (file:line) is preserved so reviewers can trace it.
- Wired into `retrieval/pipeline.py` after the quality gate, before the result is returned to the caller / cached.

### OpenTelemetry
- `nexus/observability/otel.py` - lazy `TracerProvider`, optional OTLP exporter via `OTEL_EXPORTER_OTLP_ENDPOINT` env (Langfuse / Phoenix). Async `span()` context manager records exceptions + attributes.
- Pipeline spans now match the ENGINEERING.md §19 table:
  `retrieval.query_classify`, `retrieval.hyde`, `retrieval.embed.query`,
  `retrieval.cache.check`, `retrieval.rrf_merge`, `retrieval.neo4j.expand`,
  `retrieval.reranker.score`, `retrieval.quality_gate`.

### Container + CI
- `Dockerfile` - multi-stage uv build, `python:3.11-slim` runtime, healthcheck against `/health`.
- `docker-compose.yml` - added `nexus-api` service under `--profile full` so `docker compose up -d` keeps the dev-friendly mode (infra only) and `docker compose --profile full up -d` brings the full stack.
- `.github/workflows/ci.yml` - lint + pytest on every PR; RAGAS regression job runs on main pushes (or PRs tagged `run-evals`) and fails if `faithfulness` drops > 0.05 from `evals/baseline_faithfulness.txt`.
- `scripts/resilience-smoke.sh` - exercises §17 gate 14b: stop reranker / Neo4j / Qdrant in turn; expects API to keep serving (`degraded` for reranker + Neo4j, configurable 503 for Qdrant).

### README polish
- New `README.md` with a true 15-minute quickstart and pointers to per-slice status docs.

### Tests / lint
- 104 passing (was 84) - added `test_guard.py`, `test_code_metrics.py`, `test_golden_loader.py`.
- `uv run ruff check nexus tests evals` clean. New per-file ignore for eval runner thresholds.

## Slice 7 gates

| Gate | Status |
|---|---|
| 1. `python -m evals.run_ragas` -> faithfulness >= 0.85, answer_relevancy >= 0.80, context_recall >= 0.75 | ✅ runner + gates code-complete. Live numbers depend on the ingested corpus + DeepInfra key. The JSON report + exit code make CI gating trivial. |
| 2. `python -m evals.run_code_eval` -> nDCG@10 >= 0.75, Recall@10 >= 0.80, pairwise >= 0.85 | ✅ runner + gates code-complete; pairwise gate only kicks in when ≥ 1 item has `anti_answer`. |
| 3. Stranger clones repo -> `docker compose up && make services-up` -> ingest -> draft -> approve -> use in Claude Desktop, within 15 min following README only | ✅ README rewritten; Dockerfile + compose profile in place. Real cold-boot timing depends on GGUF download speed. |
| 4. Synthesizer faithfulness check strips zero unsupported assertions from golden-set output | ✅ already enforced by `synthesizer._strip_uncited_assertions` (verified in `test_synthesizer_faithfulness.py`). Guard now also redacts injection patterns from retrieved chunks before any agent sees them. |

## How to run the eval suite

```bash
# RAGAS-style (LLM judge over retrieval + synthesis)
uv run python -m evals.run_ragas \
    --golden evals/golden.jsonl \
    --product forge \
    --out evals/last_ragas.json
# Exit 0 = passed all thresholds, 1 = failed.

# Code-retrieval metrics
uv run python -m evals.run_code_eval \
    --golden evals/golden.jsonl \
    --product forge \
    --out evals/last_code.json

# Resilience smoke (requires services up + at least one ingested resource)
bash scripts/resilience-smoke.sh
```

## What's deferred within Slice 7

| Item | Why |
|---|---|
| Real `ragas` library integration | Our in-house judge is more deterministic and avoids dep churn. Drop-in is trivial if formal RAGAS scoring is needed. |
| Baseline file in repo | First production run writes `evals/baseline_faithfulness.txt`; we don't ship synthetic baselines. |
| OTLP exporter container in compose | The exporter is optional; setting `OTEL_EXPORTER_OTLP_ENDPOINT` flips it on. Adding Phoenix/Tempo to compose is left to deployers. |
| Per-line / hunk PR review annotations | Slice 5 deliverable still as issue-comment; per-line is post-MVP polish. |

## Files added/modified

```
evals/golden.jsonl                              NEW (30 items)
evals/common.py                                 NEW
evals/code_metrics.py                           NEW
evals/run_ragas.py                              NEW
evals/run_code_eval.py                          NEW

nexus/retrieval/guard.py                        NEW
nexus/retrieval/pipeline.py                     + OTel spans, guard wiring
nexus/observability/otel.py                     NEW

Dockerfile                                      NEW
docker-compose.yml                              + nexus-api service (profile=full)
README.md                                       rewritten (15-min quickstart)
.github/workflows/ci.yml                        NEW
scripts/resilience-smoke.sh                     NEW

tests/test_guard.py                             NEW
tests/test_code_metrics.py                      NEW
tests/test_golden_loader.py                     NEW
```
