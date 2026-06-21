# Contributing to Nexus

> **Audience:** software engineers comfortable with basic LLM / RAG concepts
> (embeddings, chunking, retrieval).
>
> **Goal:** orient you in the codebase, walk you through the end-to-end flow,
> and get you to your first PR.

## 0. Before you start

Read these in order:

1. [`README.md`](./README.md) — what Nexus is + local setup.
2. [`AGENTS.md`](./AGENTS.md) — the invariants you cannot break.
3. [`ENGINEERING.md`](./ENGINEERING.md) — the formal spec. Skim §1-5 now;
   come back as needed.

## 1. Mental model

Nexus is a **per-product RAG + curation pipeline** with a hard human-in-the-
loop gate. The flow has three big phases.

**Phase 1 — Ingest (continuous).** A source yields resources. Nexus first
computes a source manifest diff in SQLite (`added`, `updated`, `removed`,
`unchanged`) using canonical resource URIs + SHA-256 content hashes +
embedding config version. Unchanged resources are skipped. Changed resources
are chunked on semantic boundaries (tree-sitter for code, headings for docs),
embedded dense + BM25-sparse, and upserted into Qdrant. Optional LLM enrichment
(HQE for code; Anthropic Contextual Retrieval for docs) is disabled by default;
if enabled, source sync queues it as background work after the fast raw index is
written. Only after the new vectors are safely written do we delete stale old
chunk IDs. A tree-sitter repo map is also persisted for council prompt context.

**Phase 2 — Council (on demand).** A user clicks "Run Council" on a topic.
A bounded LangGraph product-skill council runs: Planner retrieves evidence and
outlines one `product_master` skill; architect, domain_expert, and
quality_expert produce compact JSON reports; Synthesizer writes one Markdown
draft; Repair validates and fills missing sections or factual citation gaps;
Eval runs deterministic checks before Finalizer queues one proposal in SQLite. Human approval is still
required before anything becomes a skill file.

**Phase 3 — Approve & serve.** The user reviews + edits + approves the
proposal in the UI. `approve_proposal()` writes the Agent Skills `SKILL.md` to the
skills repo, commits + pushes, embeds the body so the skill itself becomes
retrievable, and flips the proposal status. From that point the skill is
served via MCP to any AI client connected to the product.

## 2. The two invariants

1. **Product = root entity.** Every chunk, proposal, session, and skill
   carries `product_id`. There is no cross-product read path. Business units
   are metadata only in v1 (`owner.team`), not a tenancy boundary.
2. **Humans approve, agents draft.** The council produces *proposals*, not
   skill files. Only `approve_proposal()` writes to the skills repo.

Code that breaks either invariant is a bug, regardless of how clever it is.

## 3. Backend code map (`nexus/`)

```
api/
  app.py            FastAPI app + CORS + router includes
  deps.py           Singletons (ProposalQueue, Registry, SkillStore)
  routes/
    products.py     /me, /products, /products/{id}, /products/{id}/status
    sources.py      /products/{id}/sources, SSE /sources/{id}/log
    council.py      /products/{id}/council/sessions, SSE replay
    proposals.py    /proposals, approve/reject/edit
    skills.py       /products/{id}/skills, /skills/{id}{,/corrections,/rejections,/council-history}
    dashboard.py    /products/{id}/dashboard (pipeline + pending + recentActivity)
    setup.py        /setup/status, /setup/skills-repo

ingest/
  chunker.py        tree-sitter (Py/TS/TSX/JS/Rust/Go) + heading-aware markdown
  enricher.py       optional HQE for code | Anthropic Contextual Retrieval for docs
  embedder.py       OpenAI-compatible embedder client (cloud or local llama.cpp)
  indexer.py        Qdrant upsert + delete-by-id (dense + BM25)
  pipeline.py       run_ingest() — manifest diff → chunk → embed → sparse → upsert → cleanup
  enrichment_worker.py  durable optional background enrichment
  incremental.py    legacy per-resource reindex path used by the daemon
  models.py         Chunk, ResourceRef, ChunkKind, EmbeddedChunk

retrieval/
  pipeline.py       retrieve() — dense + BM25 → RRF → rerank
  hybrid.py         RRF merge
  sparse.py         BM25 via fastembed (thread-pooled)
  reranker.py       OpenAI-compatible cross-encoder reranker
  repomap.py        tree-sitter symbol outline, persisted per product

council/
  graph.py          LangGraph product-skill StateGraph
  state.py          CouncilState TypedDict
  runner.py         Background asyncio task + SSE pub/sub hub
  queue.py          SQLite proposal/session tables
  skill_parser.py   Markdown parser + completeness validator
  agents/
    skill.py        Planner, experts, synthesizer, repair, eval, finalizer
    drafter.py      One retrieval call, one LLM call, markdown out, completeness gate
    critic.py       Own fresh retrieval, fixed rubric, severity routing
    reviser.py      Fires only on blocking; same markdown + continuation + gate
    _common.py      hits_to_evidence + evidence_for_prompt helpers

skills/
  models.py         Skill, SkillProposal, Citation, Critique, Provenance, compute_confidence
  store.py          YAML frontmatter + markdown body read/write
  approval.py      approve_proposal() — the single source of truth for skill writes
  git.py            commit_and_push helper

llm/
  client.py         OpenAI-compatible chat client, chat_markdown() with continuation

mcp_server/
  server.py         stdio MCP server entry point
  tools.py          find_skills, get_skill, query_code_context, hybrid_search_corpus

connectors/
  local_fs.py       Walks a directory
  mcp_client.py     Generic stdio MCP client
  manager.py        Routes (resource, reader) over the configured connectors

auth/
  token_cipher.py   Fernet encryption for connector tokens at rest

setup/
  bootstrap.py      Creates or clones the org's skills repo
  github_api.py     Thin GitHub REST client
  kv.py             SetupKV (skills_repo URL persistence)

daemon.py           Continuous index daemon — bootstrap + manager.updates() loop
registry.py         SQLite products/users/runtime-sources/sync manifests
config.py           NexusConfig + ${env} substitution
cli.py              `nexus council draft`, `nexus skill show`, etc.
```

The trees you'll touch most are `ingest/`, `retrieval/`, and
`council/agents/`. Most bugs hide there.

## 4. Frontend code map (`../nexus-ui/`)

```
app/                Next.js routes
  /                 ProjectsDashboard (org-wide product list)
  /setup            One-time skills-repo bootstrap
  /new              Create product
  /p/[product]/
    /dashboard      Pipeline cards + pending + recent activity
    /sources        Source list
    /sources/new    Add source (GitHub or local filesystem)
    /sources/[name] Source detail + live SSE sync log
    /ingest         IngestStage (stage gate)
    /council        Session list + start dialog
    /council/[id]   Live 3-pane deliberation
    /review         ReviewStage (proposal approve / reject / edit)
    /skill          SkillStage (terminal state)
    /skills         Skills list + detail pane
    /skills/[id]    Full skill detail (provenance, corrections, rejections, history)

components/
  screens/          One file per route — keeps each screen self-contained
  sources/          IngestionProgress (SSE consumer)
  atoms/            CitationChip
  shell/            TopBar, SideNav, ProductSwitcher, CommandPalette, ShortcutsHelp
  ui/               shadcn-style primitives (Button, Card, Badge, Progress, …)

lib/
  api/index.ts      One typed function per backend endpoint
  types.ts          Domain types (Skill, SkillProposal, CouncilSession, …)
  product-context.ts  React context for current product + perms
```

See [`../nexus-ui/DESIGN.md`](../nexus-ui/DESIGN.md) for the design system
rules + IA contract.

## 5. End-to-end traces

### Trace 1 — Onboard a product source + ingest

1. UI: `/new` creates a product, then `POST /products/{id}/sources` with
   `type: "github"`, a product service-account PAT, and `repos: string[]`.
   The standalone source screen can add GitHub or local filesystem later.
2. Backend: `nexus/api/routes/sources.py::add_source` upserts into the
   registry, returns the new source.
3. UI: `POST /products/{id}/sources/{name}/sync` → `syncSource(...)`.
4. Backend: `sources.py::sync_source` schedules `_sync_source_contents(...)` as a
   background task; returns immediately.
5. `_sync_source_contents`:
   - For `github`: validates every repo URL first, then shallow-clones each
     repo to a tempdir. The temp path is never stored in Qdrant; resources
     use canonical URIs like `github:owner/repo/path/to/file.py`.
   - For `filesystem`: opens the configured root.
   - `_emit("info", "Counting files…")` over an asyncio.Queue.
   - Iterates `LocalFsSource.list_resources()` to count.
   - `_emit("started", total=N)`.
   - Wraps the source in `_CanonicalProgressSource`, which maps real local
     files to stable resource URIs and emits `progress` events per read.
   - `await run_ingest(..., registry=registry, source_key=...)` — the delta
     pipeline. It loads previous manifest rows from SQLite, reads resources
     to hash them, skips unchanged rows, embeds only added/updated rows,
     deletes removed resources, optionally queues enrichment, and updates the
     manifest only after Qdrant writes/deletes succeed.
   - After success: builds + persists the repo map; emits `success`.
6. UI consumes `sourceLogUrl(productId, sourceId)` via `EventSource`. The
   `IngestionProgress.tsx` component renders the bar + log. SSE events are
   plain JSON with `level`, `stage`, `msg`, `ts`, and optional counters. Key
   stages: `read`, `diff`, `skip`, `chunk`, `enrich`, `embed`, `sparse`,
   `upsert`, `cleanup_stale`, `delete_removed`, `manifest_update`,
   `enrichment_queue`, `complete`.

### Trace 1b — Why resync is delta-safe

Qdrant is a derived index, not the source of truth. The source of truth for
sync state is SQLite:

- `source_resources(product_id, source_key, resource_uri, content_hash, ...,
  chunk_ids_js, embedding_version, enrichment_version, enrichment_status)`
  stores the last successfully indexed version of each resource plus optional
  enrichment state.
- `source_sync_runs(...)` stores one row per sync attempt and its final diff
  counts.

On each resync, `run_ingest()` does:

1. Load existing manifest rows for `(product_id, source_key)`.
2. List current resources and read each one once to compute a SHA-256 hash.
3. Classify:
   - **added**: no manifest row.
   - **updated**: hash changed, or embedding config version changed.
   - **unchanged**: hash + embedding version match; skip chunk/embed. If
     enrichment is enabled and its version changed, queue background enrichment
     without rebuilding the raw index.
   - **removed**: manifest row is absent from the current source listing.
4. For added/updated: chunk → dense embed → BM25 sparse encode → Qdrant
   upsert. Then delete stale old chunk IDs not present in the new chunk set.
   Then upsert the manifest row. If enrichment is enabled for that resource
   kind, queue a durable enrichment job.
5. For removed: delete old chunk IDs from Qdrant. Only then delete the
   manifest row, so a failed delete retries on the next sync.

This ordering prevents knowledge-base poisoning:

- Old vectors remain available until replacement vectors are successfully
  written.
- Failed embeds do not update manifest rows, so retry still sees the resource
  as changed.
- Deleted files do not leave orphan chunks once Qdrant deletion succeeds.
- GitHub temp clone paths do not leak into Qdrant payloads.

### Trace 2 — Run a council session

1. UI: `POST /products/{id}/council/sessions` with `{topic}` →
   `CouncilLanding.tsx` dialog.
2. Backend: `council.py::create_session` → `runner.kick_off(...)` creates
   an asyncio task. Returns `{session_id, status: "running"}`.
3. `runner._run_session`:
   - Publishes `session_start` event on `HUB`.
   - `initial_state(...)` builds the TypedDict.
   - Enters `council_handles(config)` context (retrieval, graph store, and chat clients).
     The skill nodes reuse the configured drafter/critic/reviser chat clients:
     planner uses drafter, experts use their role-specific clients,
     synthesizer uses synthesizer, eval uses critic, and repair uses reviser.
   - Compiles `build_graph()` with the SQLite checkpointer.
   - `compiled.astream(initial, ...)` — for each yielded node update:
     `_publish_node_delta(...)` translates state deltas into SSE events
     (`message`, `cost`, `critique`, `proposal_preview`).
   - On completion: enqueues the finalized proposal and records `proposal_id`
     plus the single-entry `proposal_ids` list.
   - On any node exception: the run is recorded as `failed`, an `error` event
     is streamed, and no proposal is enqueued. Council is all-or-none.
4. UI's `CouncilSession.tsx` consumes `sessionStreamUrl(sid)` via SSE:
   - Live mode while running; deterministic replay after completion
     (see `council.py::session_stream`).

### Trace 3 — Synthesizer writes the product skill

`nexus/council/agents/skill.py`:

1. `retrieve(ctx, product_id, topic, top_k=20, mode="auto")` →
   `RetrievalResult.hits`. Hits become planner `EvidenceChunk`s.
2. `load_repo_map_for_product(config, product_id)` →
   `repo_map.render(...)`. Planner and Synthesizer use this structure with
   retrieved evidence.
3. Architect, domain_expert, and quality_expert retrieve fresh evidence for
   product structure, architecture, APIs, schemas, commands, tests, standards,
   security signals, and domain language.
4. Synthesizer emits one Markdown draft with `chat.chat_markdown(...)`; long
   outputs auto-continue on `finish_reason="length"`.
5. `validate_skill_markdown(body, tier=...)` → missing/short sections and
   uncited factual sections trigger targeted repair, capped at 3 attempts per
   skill. Procedural sections such as debugging and review checklists do not
   need citations unless they assert concrete product facts.
6. `strip_uncited_rules(body)` removes list items in `## Rules` that lack
   `[file: path:line]`.
7. `parse_skill_markdown(body, evidence=evidence)` — H1 → name, regex →
   citations. Finalizer converts complete drafts into proposal rows.

### Trace 4 — Approve a proposal

`nexus/skills/approval.py::approve_proposal`:

1. `queue.get(proposal_id)` — abort if missing; no-op if already approved.
2. `_row_to_skill(row, actor)` — builds the `Skill` Pydantic model.
3. `SkillStore.save(skill)` writes `<hierarchy_root>/<product>/<name>/SKILL.md`.
4. `commit_and_push(store.root, message=…)` pushes to the configured remote.
5. `_embed_skill_body(skill, ...)` — turns the body into a single doc
   chunk and upserts to Qdrant so the skill itself becomes retrievable.
6. `queue.update_status(proposal_id, status="approved", actor=actor)`.

## 6. Local development workflow

### Backend

```bash
uv sync
cp nexus.yaml.example nexus.yaml
cp .env.example .env       # fill DEEPINFRA_API_KEY at minimum
make services-up           # Qdrant + optional local llama.cpp embedder/reranker
uv run uvicorn nexus.api.app:app --port 8000 --reload
```

Default `nexus.yaml.example` uses Qdrant plus DeepInfra Qwen embedding/rerank
models, so low-resource machines do not need local model servers. Use
`make local-models-up` when testing the optional `jina-local` profile
(llama.cpp serving local Jina v4 embeddings + Jina Reranker v3 for offline/high-resource machines). Qdrant v1.18+ native
TurboQuant is controlled by `vector_store.quantization`; changing quantization,
embedding dimension, or collection names requires resync/reindex.

On Apple Silicon, `scripts/serve-embedder.sh` and `scripts/serve-reranker.sh`
auto-detect Metal (`--n-gpu-layers 999`). On non-GPU machines they fall back
to CPU. Useful knobs:

```bash
EMBEDDER_DEVICE=cpu RERANKER_DEVICE=cpu make services-up   # force CPU
EMBEDDER_UBATCH=2048 RERANKER_UBATCH=2048 make services-up # larger RAM machine
NEXUS_LOG_LEVEL=DEBUG uv run uvicorn nexus.api.app:app --port 8000 --reload
```

Default local embedder physical batch is intentionally conservative (`1024`) for
a MacBook Air M2 with 8GB RAM. The default DeepInfra ingest path is more
parallel now that enrichment is off: `embed_batch_size=32`,
`file_batch_size=50`, `read_concurrency=10`, and `batch_concurrency=2`.

### Retrieval model config

When switching embedding or reranking providers, update config deliberately:

- set `models.embedding.dim` to the provider's output dimension before creating
  Qdrant collections
- set `models.embedding.instruction_profile` to the prompt/prefix scheme used
  for query and passage embeddings
- reset `ingestion.quality_gate_threshold` to `0.0` until eval calibrates the
  new reranker score scale
- resync/reindex products after embedding provider/model/dim/profile changes

Nexus does not currently process visual Confluence diagrams or image
attachments. Text around a diagram may be indexed, but boxes/arrows/labels in
the image are not extracted, embedded, or cited.

### Frontend

```bash
cd ../nexus-ui
npm install
npm run dev                # http://localhost:3000
```

### Tests + lint

```bash
uv run ruff check nexus tests           # must be clean
uv run pytest -q                        # unit + integration
uv run pytest -m eval                   # opt-in retrieval benchmark
                                        #   (skips if Qdrant/embedder/reranker absent)
uv run python -m evals.run_ragas        # RAGAS-style golden eval
uv run python -m evals.run_code_eval    # manual code retrieval eval
make test-live-e2e                      # live Qdrant E2E
```

The retrieval eval is the floor for "did my change to the pipeline make
things better or worse?". Re-run it after any modification to chunker,
optional enricher, hybrid, rerank, repomap, or contextual-retrieval logic.
Qdrant with native TurboQuant is the default and only vector-index path; eval
decisions compare the current stack against the published floors.

### CLI

```bash
# Run a council draft from the CLI (no UI):
uv run nexus council draft --product my-api --topic "auth middleware"

# Dry-run product cleanup:
uv run nexus delete-product --product my-api

# Delete product-scoped SQLite rows, skills, repo map, checkpoints, and index entries:
uv run nexus delete-product --product my-api --yes
```

`--skip-qdrant` keeps the command usable when the retrieval backend is offline,
but leaves derived index entries behind until you clean that product later.

### MCP server

```bash
uv run nexus-mcp-server --product my-api
# Add an entry in Claude Desktop's claude_desktop_config.json — see README.
```

## 7. Hands-on tour

Five tinkering exercises to grow your intuition. Do them in order.

**1. Reduce a chunker constant and watch the test fail.** Edit
`nexus/ingest/chunker.py` and set `MAX_CHUNK_CHARS = 200`. Run
`uv run pytest tests/test_chunker.py -q`. Read the failure. Revert.

**2. Add a query to the eval set + watch the floor break.** Edit
`tests/eval/queries.json` and add a query pointing at a file you know
isn't well-indexed (e.g. a bullet-list-only doc). Run
`uv run python -m tests.eval.harness --product my-api` against your
populated index. Watch recall drop. Revert (or fix the pipeline).

**3. Trace a Synthesizer call through the LLM client.** In
`nexus/llm/client.py::chat_markdown`, add a `log.info(...)` at the top of
the continuation loop. Run a council session via the UI. Watch the logs.
Remove the log statement.

**4. Add a connector option to the UI.** In
`nexus-ui/components/screens/ConnectorNew.tsx`, find the `CONNECTOR_OPTIONS`
list. Add a stub option only when the backend can sync it. Confluence/Jira are
planned as product-scoped source configs, but should not appear until wired.
Roll back.

**5. Read the repo map your own ingest built.** Cat
`./data/repomaps/<your-product>.json` and find your favourite class. You
should see its line number + signature.

**6. Watch delta sync skip unchanged files.** Sync a product twice without
changing files. The second SSE stream should show `skip` events and final
`unchanged=N`, with no chunk/embed work for unchanged resources. If optional
enrichment is enabled and only the enrichment version changed, the raw index is
left alone and a background enrichment job is queued.

## 8. Recipes

### Add a new API endpoint

1. Create the handler in `nexus/api/routes/<area>.py` using an existing
   router (e.g. `router = APIRouter(tags=["foo"])`).
2. Include the router in `nexus/api/app.py` if it's a new file.
3. Add the typed client in `nexus-ui/lib/api/index.ts`.
4. Add a screen that calls it (or wire it into an existing screen).
5. Add an integration test under `tests/test_<area>.py` using `TestClient`
   and `dependency_overrides`.

### Add a new tool to the MCP server

1. Define the handler in `nexus/mcp_server/tools.py` (async function
   `(state, **kwargs) -> dict`).
2. Register the schema + dispatch in `nexus/mcp_server/server.py`'s
   `list_tools()` + `call_tool()`.
3. Add a test against the helper that doesn't require a live MCP client.

### Add a new chunker language

1. Add the tree-sitter package to `pyproject.toml`.
2. Extend `nexus/ingest/chunker.py::_LANGS` with the new `_LangCfg`.
3. Extend `_lang_for()` to map the extension.
4. Mirror the new node types in `nexus/retrieval/repomap.py::_KIND_BY_NODE`
   so the repo map captures them too.
5. Add a test in `tests/test_chunker.py` with a small sample file.

### Tune the council prompts

The drafter / critic / reviser system + user templates live near the top of
each agent file. Edits land in production immediately on the next council
run; no schema change required. After a prompt edit, **always** re-run
`pytest -m eval` and at least one real council session to sanity-check the
output shape (citations, completeness gate).

## 9. Testing

Four tiers plus one CI-only regression gate.

**Unit tests** (`tests/test_*.py`) — pure logic. Run `uv run pytest -q`.
Should pass in seconds, no external infra.

**Integration tests with TestClient** (some files under `tests/`) — exercise
FastAPI routes with `dependency_overrides`. Still no live infra.

**Retrieval eval** (`tests/eval/`) — opt-in via `pytest -m eval`. Requires
a populated Qdrant index + reachable embedder + reranker. The pytest
wrapper probes infra and `pytest.skip`s cleanly when anything's down so
the marker is safe to leave in CI behind a conditional.

**RAGAS-style golden eval** (`evals/run_ragas.py`) — runs retrieval over
`evals/golden.jsonl`, synthesizes short answers from retrieved contexts, and
uses the configured council model as a judge. Gates are faithfulness `>= 0.85`,
answer relevancy `>= 0.80`, and context recall `>= 0.75`. CI runs this in the
`ragas-regression` job when `DEEPINFRA_API_KEY` is configured, with `--limit 10`
against the seed Forge skills, and uploads `evals/ci_ragas.json`.

**Code retrieval eval** (`evals/run_code_eval.py`) — manual golden-set runner
for nDCG@10 `>= 0.75`, Recall@10 `>= 0.80`, and pairwise preference accuracy
`>= 0.85` when a golden item supplies an `anti_answer`. It is not wired into CI.

### Conventions

- Tests for new public leaf functions and new API routes are required.
- Prefer asyncio integration tests over heavy mocking; the FastAPI
  `TestClient` is fast enough that mocking the queue + store directly
  is rarely worth it.
- Use `tmp_path` for filesystem state.
- Use `httpx.MockTransport` for outbound HTTP (see
  `tests/test_enricher.py` for the pattern).

## 10. Code conventions

- **Python 3.13+, `uv` for everything.** `from __future__ import annotations`
  at the top of every module.
- **Pydantic** for boundary types; **dataclasses** for in-memory state;
  plain dicts only in tests.
- **Async by default** for anything touching the network or doing
  significant I/O.
- **No `print()`.** Use `log = logging.getLogger(__name__)`. API startup
  calls `nexus.logging_config.setup_logging()`, and `NEXUS_LOG_LEVEL=DEBUG`
  is the normal way to turn up backend detail.
- Import order: stdlib → third-party → `nexus.*`. Ruff/isort enforces.
- **Type-annotate everything** that crosses a function boundary. The
  ergonomic value of annotations on locals is debatable; on signatures
  it's not.
- One logical change per commit; imperative subject.

## 11. Glossary

| Term | Meaning |
|---|---|
| **product** | Root entity; everything below it (sources, chunks, sessions, skills) is scoped to one product. |
| **business unit** | Optional display metadata on a product (`owner.team`) in v1; not a route, table, or isolation boundary. |
| **skill** | A curated, human-approved Agent Skills `SKILL.md` playbook with file:line citations. Lives in the org's skills repo. |
| **proposal** | The council's draft of a skill. Lives in SQLite until a human approves / rejects / edits. |
| **session** | One council run; produces one proposal. |
| **chunk** | A slice of an ingested resource (function, class, paragraph). Has `kind` ∈ {CODE, DOC}, `context_path`, `context_summary`. |
| **manifest** | SQLite sync state for a source resource: canonical URI, content hash, chunk IDs, embedding version, optional enrichment version/status. Prevents full re-embed and stale-vector poisoning. |
| **source_key** | Stable per-product/per-source/per-root key used to group manifest rows. GitHub multi-repo sources get one key per repo. |
| **canonical URI** | Stable `resource_uri` stored in Qdrant and the manifest. Filesystem uses absolute paths; GitHub uses `github:owner/repo/path`. |
| **HQE** | Optional Hypothetical Question Embeddings — 3 questions a developer would type to find a code chunk, prepended at embed time. Disabled by default. |
| **Contextual Retrieval (CR)** | Optional Anthropic-style "situate this chunk within the document" prefix; the doc analogue of HQE. Disabled by default. |
| **repo map** | tree-sitter symbol outline of a product's source tree, injected into council system prompts. |
| **RRF** | Reciprocal Rank Fusion — how dense and sparse retrieval hits are combined. |
| **Planner / Experts / Synthesizer / Repair / Eval / Finalizer** | The bounded product-skill council nodes. |

## 12. Further reading

- [`README.md`](./README.md) — quickstart.
- [`AGENTS.md`](./AGENTS.md) — invariants.
- [`ENGINEERING.md`](./ENGINEERING.md) — the formal spec.
- [`../nexus-ui/DESIGN.md`](../nexus-ui/DESIGN.md) — UI design system.
- Reflexion paper (Shinn et al. 2023, `arxiv.org/abs/2303.11366`) — the
  draft/critique/revise pattern Nexus's council follows.
- Anthropic Contextual Retrieval (Sep 2024,
  `anthropic.com/news/contextual-retrieval`) — the optional doc enrichment
  technique.
- aider repo map (`aider.chat/docs/repomap.html`) — the symbol-outline
  technique Nexus's repomap is modelled on (minus PageRank for v1).
