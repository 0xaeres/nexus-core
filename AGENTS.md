# Nexus — agent & contributor context

This is the Python backend for **Nexus**, a sovereign, MCP-native **context
engine**: it ingests an org's code + docs, runs a bounded expert LLM council to
draft one curated **product skill** (human-approved), and serves it via MCP to any AI
client. The sibling repo `../nexus-ui/` is the Next.js web UI.

## Read first

| Doc | What it is |
|---|---|
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | **The contributor guide — start here.** Concepts, code map, end-to-end traces, local dev, recipes. |
| [`ENGINEERING.md`](./ENGINEERING.md) | The formal spec — architecture, data model, API contracts. |
| [`README.md`](./README.md) | Setup + quickstart. |

## The two invariants — never break these

1. **Product = root entity.** Every resource carries `product_id`; Qdrant
   payload filters/indexes on it. Code that crosses the product boundary is a
   tenancy bug.
   Business units are metadata only in v1 (`owner.team`); do not add BU routes,
   tables, or tenancy semantics without a product decision.
2. **Humans approve, agents draft.** The council writes *proposals*. Nothing
   becomes a skill file without an explicit human approval.

## Pipeline shape — keep this honest

- **Resync is delta-only.** Every sync computes `{added, updated, removed,
  unchanged}` from the SQLite source manifest. Unchanged resources are skipped;
  changed resources are re-embedded before stale old chunk IDs are deleted.
  Don't reintroduce blind full-source upserts.
- **Retrieval is three stages: dense + BM25 → RRF → configured reranker.** No
  classifier, no HyDE, no semantic cache, no graph expansion, no circuit
  breakers. Don't reintroduce them without an eval-set win to justify it.
- **Chunks carry their context.** Code chunks get HQE (3 hypothetical
  questions) at ingest; doc chunks get Anthropic's Contextual Retrieval
  blurb. Both prepend at embed time via `text_for_embedding()`.
- **Council is bounded single-skill generation.** Planner → expert fanout
  (architect, domain_expert, quality_expert) → Synthesizer →
  completeness Repair (≤3 attempts per skill) → Eval → Finalizer.
  Each expert produces a compact JSON report (summary, findings,
  missing_questions); the Synthesizer builds the full 13-section
  `product_master` Markdown skill from those reports + evidence + repo map.
  Incomplete skills are never queued; the Eval node runs 5 deterministic
  checks (identity, structure, name match, citation faithfulness, trigger).
- **Synthesizer emits Markdown skills, not JSON.** Citations are regex-parsed
  post-hoc. Long outputs auto-continue on `finish_reason="length"`. Missing
  sections trigger targeted section-fill repair, capped at 3 attempts per skill;
  incomplete skills are never queued.
- **Repo map** lives in the council system prompt: a tree-sitter symbol
  outline of the source tree, lexically ranked against the session topic,
  token-budgeted. Built at sync time, persisted under
  `<state>/repomaps/<product>.json`.
- **Evidence chunks per session is capped** at `EVIDENCE_CHUNKS_PER_SESSION_CAP = 20`
  in [`nexus/council/agents/skill.py`](./nexus/council/agents/skill.py).

## Conventions

- **Python 3.13+, managed by `uv`.** `from __future__ import annotations` at
  the top of every module.
- **Pydantic** for data crossing process boundaries; **dataclasses** for
  in-memory only; plain dicts only in tests.
- **Async by default** for anything touching the network or significant I/O.
- Import order: stdlib → third-party → `nexus.*` (ruff enforces).
- No `print()` — use `log = logging.getLogger(__name__)`.

## Before you commit

```bash
uv run ruff check nexus tests        # lint — must be clean
uv run pytest -q                     # tests — must be green (146 at last count)
```

The retrieval eval (`pytest -m eval`) is opt-in — it skips when
Qdrant/embedder/reranker aren't reachable. Run it after any retrieval-stack
change against a live product index.

- Add a test for every new public leaf function and every new API route.
- One logical change per commit; imperative subject.

## Don't

- Don't add runtime dependencies without discussion — the dep set is deliberate.
- Don't seed demo/placeholder products — the system boots empty; users onboard
  their own via the wizard. Product onboarding creates a required GitHub
  source with a product service-account PAT and one or more repo URLs.
- Don't skip the proposal/approval step for any write action (Invariant 2).
- Don't reintroduce the cut layers (Assistant / Jira / Confluence, Neo4j /
  GraphRAG, org library / composition / SkillKind, HyDE / classifier /
  cache / circuit breakers) without an eval-set or feedback win to justify
  the complexity.
- Don't store secrets in plaintext — connector tokens are Fernet-encrypted
  (`NEXUS_TOKEN_KEY`). Credentials are scoped per product source, not as a
  global or product-wide credential bundle.
