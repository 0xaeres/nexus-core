# Nexus — Engineering Reference

The formal spec. Data model, pipeline shapes, API contracts. Code-first; if a
behaviour diverges from this doc, the code is the source of truth and the doc
needs a patch.

## Overview

Nexus indexes a product's code + docs, runs a bounded expert council to draft
a product skill pack, requires humans to approve proposals, and serves the
resulting skills (and the raw corpus) over MCP to AI coding clients.

The system is deliberately small. Anything that doesn't move the
`recall@10` / `MRR` number on `tests/eval/queries.json` or fix a real
ergonomic pain point should not be in the codebase.

```
ingest                                              council
─────────────                                       ─────────────
chunker         tree-sitter / heading-aware         drafter
enricher        optional HQE / Anthropic CR         critic   ← own retrieval
embedder        Jina v4 (Metal)                     reviser  ← only on blocking
indexer         Qdrant (dense + BM25)                  │
repomap         tree-sitter symbol outline             ▼
                                                    SkillProposal
                                                       │
retrieval       dense + BM25 → RRF → rerank            ▼
                                                    human approval
mcp_server      find_skills, query_code_context,       │
                hybrid_search_corpus                   ▼
                                                    skill.md committed
                                                       │
                                                       ▼
                                                    MCP serves it
```

## 1. Invariants

Two hard constraints. Code that violates them is a bug.

1. **Product = root entity.** Every chunk, proposal, session, and skill
   carries `product_id`; Qdrant shards on it. There is no cross-product
   read path. Crossing the boundary is a tenancy bug.
   Business units are optional product metadata (`owner.team`) in v1, not a
   first-class entity or tenancy boundary.
2. **Humans approve, agents draft.** The council emits `SkillProposal`s.
   Nothing becomes a `SKILL.md` on disk or in Git without
   `approve_proposal()` being called by an authenticated actor.

## 2. Data Model

All cross-process boundary types are Pydantic. In-memory-only types use
`@dataclass`. Tests may use plain dicts.

### Skill (`nexus/skills/models.py`)

The unit of curated guidance Nexus serves to agents.

```python
class Skill(BaseModel):
    name: str
    description: str
    product: str
    version: int = 1
    confidence: float          # [0.0, 1.0]
    applies_to: AppliesTo      # files: list[str], contexts: list[str]
    provenance: Provenance
    body: str                  # markdown body (no frontmatter)

    @property
    def id(self) -> str:
        return f"{self.product}/{self.name}"
```

On disk: `<hierarchy_root>/<product>/<name>/SKILL.md` with Agent Skills
frontmatter (`name`, `description`, `compatibility`, and `metadata.nexus_*`).
Legacy `<product>/<name>.skill.md` files remain readable.

```python
class Provenance(BaseModel):
    council_session: str | None
    validated_by: str
    validated_at: str          # ISO-8601
    evidence_chunks: list[str]
    adversary_critique: str | None  # short note from the Critic, if any
    revision_count: Literal[0, 1]   # session-scoped, fed into the confidence formula
```

`revision_count` is hard-capped at `0 | 1` because the council's revision
loop fires at most once (see §5). The confidence formula:

```
confidence = citation_density * critic_passes
  where citation_density = min(citations / paragraphs, 1.0)
        critic_passes    = 1.0 if revision_count == 0 else 0.7
```

### SkillProposal (`nexus/skills/models.py`)

Council output queued for human review. Persisted in the
`proposals` SQLite table (`nexus/council/queue.py`).

```python
class SkillProposal(BaseModel):
    id: str
    name: str
    body: str
    citations: list[Citation]
    confidence: float
    adversary_critique: Critique | None
    status: Literal["pending", "approved", "rejected", "edited"]
    created_at: str
    approved_by: str | None
    approved_at: str | None
```

### Citation

```python
class Citation(BaseModel):
    id: str | None             # chunk id when the citation maps to an indexed chunk
    file: str
    line: int
    excerpt: str
```

Citations are post-hoc parsed from the proposal body by
`nexus/council/skill_parser.py` (regex on `[file: path:line]` markers).

### Chunk (`nexus/ingest/models.py`)

```python
class Chunk(BaseModel):
    product_id: str
    resource: ResourceRef
    content: str
    start_line: int            # 1-indexed
    end_line: int
    kind: ChunkKind            # CODE | DOC
    context_path: str = ""     # heading hierarchy for docs (e.g. "Auth / API Keys / Rotating")
    context_summary: str = ""  # optional enricher output: HQE questions OR CR context

    @property
    def id(self) -> str:       # deterministic UUID5 over product + uri + start:end
        ...

    def text_for_embedding(self) -> str:
        if self.context_summary:
            return f"{self.context_summary}\n\n{self.content}"
        if self.context_path:
            return f"{self.context_path}\n\n{self.content}"
        return self.content
```

Chunks are content-addressed via UUID5 so re-ingest is idempotent — the same
file at the same lines produces the same chunk id every time, and re-indexing
overwrites the same Qdrant point.

## 3. Ingestion Pipeline

`nexus/ingest/pipeline.py::run_ingest()` orchestrates the per-product flow.
When called from the source sync route it receives the SQLite `Registry` and a
stable `source_key`, which enables delta-safe resync. The pipeline does:

```
list resources ──► read + SHA-256 hash ──► manifest diff
  ├─ unchanged ──► skip
  ├─ removed   ──► Qdrant delete IDs ──► delete manifest row
  └─ added/updated ──► chunk ──► dense embed ──► BM25 sparse
                    ──► Qdrant upsert ──► delete stale old IDs
                    ──► upsert manifest row ──► optional enrichment queue
```

Files batch in groups of `IngestionCfg.file_batch_size` (default 50). Reads are
concurrent at `IngestionCfg.read_concurrency` (default 10). Changed batches are
processed concurrently up to `IngestionCfg.batch_concurrency` (default 2), and
the embedder is called once per changed batch, never for unchanged resources.

### Delta-Safe Manifest (`nexus/registry.py`)

Qdrant is a derived index. SQLite is the source of truth for what has been
successfully indexed:

- `source_resources` primary key: `(product_id, source_key, resource_uri)`.
  Stores `content_hash`, `mime`, `size_bytes`, `last_seen_sync`,
  `chunk_ids_js`, `indexed_at`, `embedding_version`, `enrichment_version`,
  and `enrichment_status`.
- `enrichment_jobs` stores durable optional enrichment work. Jobs are claimed
  by the FastAPI lifespan worker, dropped if stale, retried up to the configured
  cap, and deleted after success.
- `source_sync_runs` stores sync attempt start/finish timestamps, diff counts,
  and status.

`embedding_version(config)` hashes retrieval backend, embedding
provider/model/endpoint/dimension/instruction profile and Qdrant quantization
settings. If any of those change, rows classify as `updated` and re-embed on
the next sync.

`enrichment_version(config)` hashes the light enricher model/endpoint and
enrichment toggles. If optional enrichment is enabled and this version changes,
unchanged resources are queued for background enrichment without rebuilding the
raw dense+BM25 index.

Ordering rules:

1. Added/updated resources: write replacement index points first.
2. Delete old stale chunk IDs only after replacement upsert succeeds.
3. Update manifest row only after index upsert + stale cleanup succeeds.
4. Removed resources: delete index IDs first, then delete manifest row.

These rules prevent poisoning: failed embeds keep old good vectors and manifest
state; failed deletes keep manifest rows so cleanup retries later.

### Chunker (`nexus/ingest/chunker.py`)

- **Code**: tree-sitter (Python, TS, TSX, JS, Rust, Go). Chunk boundaries
  are function / class / impl / trait / interface / method nodes. Oversized
  bodies fall through to a char splitter with overlap.
- **Markdown**: heading-aware splitter. Each chunk's `context_path` carries
  its heading hierarchy (e.g. `"Auth / API Keys / Rotating"`).
- **Plain text**: char splitter with overlap.

Sizing (defaults target a MacBook Air M2/8GB running llama.cpp with
`EMBEDDER_UBATCH=1024`):

```
MAX_CHUNK_CHARS    = 1200   # ~300-400 tokens; leaves room for optional prefixes
CHAR_SPLIT_TARGET  = 700
CHAR_SPLIT_OVERLAP = 70
```

Don't raise these without checking llama.cpp physical batch errors in the
embedder log. For larger machines, raise `EMBEDDER_UBATCH`/`EMBEDDER_BATCH`;
for constrained machines, lower chunk sizes instead.

### Enricher (`nexus/ingest/enricher.py`)

Optional LLM enrichment is available but disabled by default
(`ingestion.enrich_chunks.docs: false`, `code: false`). The default ingest path
is raw dense + BM25 indexing so a Nexus-sized repo becomes searchable quickly.
If enabled, source sync writes the raw index first and queues durable background
jobs; the worker enriches changed resources and upserts the same deterministic
chunk IDs.

Two optional strategies are dispatched on `ChunkKind`:

**Code → HQE (Hypothetical Question Embeddings).** The LLM generates 3
"questions a developer would type to find this code", prefixed `Q:`. Stored
in `context_summary`, prepended at embed time. Closes the
English↔identifier gap that bare code embeddings hit.

**Docs → Anthropic Contextual Retrieval**
(`anthropic.com/news/contextual-retrieval`, Sep 2024). The LLM sees the
whole document + the chunk and writes a 50-100 token "situate this chunk
within the document" blurb. Stored in `context_summary`. Anthropic's
measured numbers on their internal eval: -35% top-20 failure rate vs. raw
embeddings; -49% combined with BM25; -67% combined with BM25 + rerank.

Per Anthropic: the whole-doc prefix is naturally amenable to server-side
prompt caching. DeepInfra / OpenAI APIs auto-dedupe the prefix across
multiple chunks of the same doc, so cost is ~$1/million chunks at Haiku
rates.

`_truncate_doc()` caps the prefix at 30 000 chars (~7.5k tokens) centred on
the chunk when a doc is pathologically large; the chunk itself is always
preserved.

`doc_contents: dict[str, str]` (uri → full text) is threaded from
`pipeline.flush()`, the background enrichment worker, and
`incremental.reindex_resource()` so the enricher has the full doc, not just the
chunk.

### Embedder (`nexus/ingest/embedder.py`)

OpenAI-compatible client against DeepInfra Qwen3 by default, or a local
llama.cpp server hosting **Jina Embeddings v4** (`jinaai/jina-embeddings-v4`,
2048-dim) when `provider: jina-local`. The
`text_for_embedding()` prefix (`context_summary` from optional enrichment, or
`context_path`) is included in the input. llama.cpp sometimes reports
physical-batch token-limit failures as
HTTP 500; `EmbedderClient` treats those as non-retryable request/config errors
instead of burning retry backoff.

Current retrieval is text/code only. Nexus does **not** process Confluence
architecture diagrams, screenshots, or image attachments as visual evidence:
it may retrieve surrounding page text, but it does not OCR/layout-parse
diagrams, embed images, extract boxes/arrows, or cite diagram regions. Add a
separate visual/document pipeline before claiming diagram support.

Embedding/reranker swaps are high-risk config changes. The brand is less
important than keeping these aligned:

- instruction profile (query vs passage prefixes, code vs docs wording)
- `models.embedding.dim` and existing vector index dimensions
- chunk size vs provider context limits
- reranker score scale and `quality_gate_threshold`
- reindex/resync after any embedding provider/model/dim/profile change

`embedding_version(config)` includes provider, model, URL/base URL, dimension,
instruction profile, and Qdrant quantization settings. If any changes, delta
ingest treats resources as updated and rebuilds chunks instead of reusing stale
vectors. Reranker, quality gate, and optional enrichment changes do not force a
raw reindex.

`scripts/serve-embedder.sh` exposes:

- `EMBEDDER_DEVICE=auto|metal|gpu|cpu` (auto: Apple Silicon → Metal,
  Nvidia → GPU, otherwise CPU).
- `EMBEDDER_BATCH` and `EMBEDDER_UBATCH` (default `1024` for M2/8GB).
- `EMBEDDER_GPU_LAYERS` for manual override.

`scripts/serve-reranker.sh` mirrors these controls as `RERANKER_DEVICE`,
`RERANKER_BATCH`, `RERANKER_UBATCH`, and `RERANKER_GPU_LAYERS`. Reranker
physical-batch failures have the same llama.cpp shape as embedder failures:
raise `RERANKER_UBATCH` on larger machines or reduce retrieved document size
if running on constrained hardware.

### Indexer (`nexus/ingest/indexer.py`)

The Qdrant indexer implements the async storage contract: ensure storage,
upsert embedded chunks, dense search, BM25 search, count, delete by chunk IDs,
delete by resource, delete by product, and close. The retrieval pipeline is
dense + BM25 -> RRF -> configured reranker.

Qdrant collections (per `nexus.yaml`):

- `nexus_code` — code chunks, dense + sparse vectors
- `nexus_text` — doc chunks, dense + sparse vectors

Both collections filter payloads on `product_id`; all retrieval paths include
that filter. Sparse vectors come from BM25 (`Qdrant/bm25` via fastembed). One
upsert per changed batch carries both dense and sparse vectors for every chunk.
Dense vectors use Qdrant native TurboQuant by default (`bits4`, `always_ram:
true`) when collections are created.

Payload fields include `product_id`, `resource_uri`, `source_id`, `source_key`,
`content_hash`, `embedding_version`, `indexed_at`, `kind`, line span,
`context_path`, and `content`. Optional enrichment overwrites the same point IDs
with enriched vectors after the raw index exists. The indexer can delete by
`resource_uri` for repair paths or by explicit chunk IDs for delta-safe stale
cleanup.

### Structural Summary Chunks (`nexus/ingest/summaries.py`)

After deterministic graph extraction, ingest writes one source-backed structural
summary chunk per resource when graph facts exist. Summary chunks use the
original `resource_uri`, line `0`, `context_path="Graph summary"`, and
`artifact_type="summary"`. They are embedded as text and carry the same
product/source/graph metadata as normal chunks, so broad architecture queries
can retrieve graph-aware overview evidence before drilling into concrete source
chunks. These summaries are deterministic and do not bypass source citations.

### Repo Map (`nexus/retrieval/repomap.py`)

Built once at sync time (while the local clone still exists), persisted to
`<state>/repomaps/<product_id>.json`. Tree-sitter walks the tree, extracting
function / class / method / struct / trait / interface / type / enum / impl
definitions with their start line and signature.

Skipped: `node_modules`, `.venv`, `.git`, `dist`, `build`, `target`, `vendor`,
`.next`, `.pytest_cache`, files > 250 KB.

At council time the map is loaded, ranked against the session topic (lexical
overlap on `name + path` plus a small structural weight: classes > functions
> methods), and rendered into a token-bounded block of `file:\n  signature
[Lline]` lines. The render is injected into planner/synthesizer prompts so the
council sees the codebase structure alongside retrieved evidence.

We deliberately skip aider's personalized-PageRank step in v1. With < 5k
files, lexical + structural ranking is within striking distance and avoids
`networkx` as a dependency. Add PR back if `tests/eval/queries.json` proves
the gap matters.

### Legacy Per-Resource Ingest (`nexus/ingest/incremental.py`)

`reindex_resource(product_id, resource, content)` is the daemon's older
per-resource repair path. It deletes existing chunks for one `resource_uri`,
then chunks, optionally enriches, embeds, and upserts the whole resource.
Product-source resyncs must use the manifest-aware
`run_ingest(..., registry=..., source_key=...)` path instead; do not reintroduce
blind full-source upserts.

## 4. Retrieval Pipeline

Nexus has two retrieval layers.

`nexus/retrieval/pipeline.py::retrieve()` remains the low-level hybrid
primitive:

```
embed(query) ──► dense top-50 ┐
                              ├── RRF merge top-20 ──► rerank top-K
BM25(query) ──► sparse top-50 ┘
```

`mode="auto"` queries both `code` + `text` collections; `mode="code"` or
`"text"` restricts. Reranker score gate (`IngestionCfg.quality_gate_threshold`,
default `0.0`) applies only after rerank succeeds: chunks with reranker scores
below the gate are dropped. Dense embedding scores, BM25 scores, and reranker
scores are separate scales; this gate is not an embedding-similarity threshold.
Keep provider defaults at `0.0` until an eval set calibrates that reranker's
score scale and proves a higher cutoff improves precision without losing
needed evidence.

`nexus/retrieval/evidence.py::retrieve_evidence()` is the core product context
engine used by council, MCP evidence search, and product chat. It runs
complementary candidate channels, then assembles a coverage-aware evidence set:

```text
query understanding
  ├─ hybrid dense+BM25+rerank
  ├─ exact indexed grep
  ├─ repo-map symbol search
  ├─ graph-local traversal -> attached chunks
  ├─ structural summary chunks
  └─ approved skill memory
      ↓
mixed rerank + dedupe + channel quotas + file diversity + coverage gate
```

Graph is a navigation layer, not an answer source by itself. Graph traversal
resolves files/symbols/routes/config keys to source-backed chunks and explains
why adjacent evidence is related. Do not pollute semantic queries by appending
all graph neighbor names. Broad product questions should retain documentation
structural summaries, and implementation evidence; exact/path/symbol matches
should not be crowded out by semantically similar but incomplete chunks. When a
reranker is configured, the evidence engine reranks the mixed candidate pool
across all channels before applying quotas.

Still out of scope without an eval-set win: HyDE, semantic cache, classifier
fallbacks, free-form code graph extraction, and circuit breakers. The retrieval
eval set (§10) is the floor; new layers must improve it.

## 5. Council — Expert Skill Pack

`nexus/council/graph.py`. LangGraph state graph for product skill packs:

```
START ──► Planner ──► Experts ──► Synthesizer ──► Repair ──► Judge
                                                               │
                                  missing evidence && cb == 0? │
                                                               ├── true ─► Targeted Callback
                                                               │            └──► Synthesizer
                                                               └── false ─► Finalizer ─► END
```

State (`nexus/council/state.py::CouncilState`):

```python
class CouncilState(TypedDict, total=False):
    session_id: str
    product_id: str
    topic: str
    config_path: str
    evidence: list[EvidenceChunk]   # reducer-merged: planner + experts + callback
    skill_plan: list[SkillPlanItem]
    expert_reports: list[ExpertReport]
    skill_drafts: list[SkillDraft]
    proposals: list[SkillProposal]  # context, architecture, engineering proposals
    judge_result: JudgeResult | None
    callback_count: int             # capped at 1
    proposal: SkillProposal | None
    proposal_id: str | None         # primary/master proposal for compatibility
    critique: Critique | None
    revision_count: int             # capped at 1
    deliberation: list[DeliberationMessage]  # append-only stream
    costs: list[AgentCost]
```

### Pack Agents (`nexus/council/agents/pack.py`)

Planner retrieves the initial evidence and creates exactly three skill
outlines: `{product}-context`, `{product}-architecture`, and
`{product}-engineering`. Expert fanout then runs two bounded lenses: Product
Mapper and Engineering Mapper. Product Mapper extracts identity, domain
vocabulary, entities, apps/services/repos, APIs, schemas, auth/tenancy, and
boundaries. Engineering Mapper extracts commands, toolchain, tests/evals, code
standards, security/secrets, debugging, and review signals. Experts do fresh
retrieval so the Synthesizer sees more than the planner's initial chunk pool.

Synthesizer emits one Markdown skill per outline. The completeness repair loop
validates every draft against the tier-specific schema and retries missing
sections or factual citation gaps up to `REPAIR_ATTEMPT_CAP = 3`. Factual
product claims require citations; procedural guidance does not unless it names
a concrete product fact. Incomplete skills are never queued: if repair still
fails, the session stops with `reason="incomplete_skill"`.

Judge checks evidence coverage and may request one targeted expert callback.
After that callback, the graph re-synthesizes and repairs. A second unresolved
evidence gap stops the session with `reason="insufficient_evidence"`.

Finalizer parses complete drafts into multiple `SkillProposal` rows. The
primary/master proposal remains available as `proposal_id` for old clients;
the full pack is persisted on the session as `proposal_ids`.

### LLM client (`nexus/llm/client.py`)

OpenAI-compatible (`/chat/completions`) async client. Three methods:

- `chat(messages, *, json_mode=False)` — single call.
- `chat_json(messages)` — adds `response_format: json_object`.
- `chat_markdown(messages, *, max_continuations=2)` — the aider/cursor
  pattern. On `finish_reason == "length"` resends the partial as an
  assistant message + `"Continue exactly where you stopped"` user message,
  concatenates the chunks. Token usage is summed.

`ChatResponse.finish_reason` and `.truncated` are exposed for callers who
need them.

### Runner (`nexus/council/runner.py`)

Background asyncio task. Streams LangGraph node updates onto a per-session
pub/sub hub (`HUB`) so SSE clients see live deliberation + cost + critique
+ proposal-preview events. On completion the proposal is enqueued and the
session row is recorded.

### Queue (`nexus/council/queue.py`)

SQLite. Two tables:

- `proposals` — one row per `SkillProposal`. Status transitions:
  `pending → approved | rejected | edited`.
- `sessions` — one row per council run. Carries `deliberation_js`,
  `costs_js`, `proposal_id`, `started_at`, `completed_at`, `status`.

The `org_proposals` + `change_requests` tables from the old org-library
flow are gone.

## 6. Approval flow

`nexus/skills/approval.py::approve_proposal()` is the source of truth. Both
the API (`POST /proposals/{id}/approve`) and the CLI call it. Idempotent
within a session — re-approving a row already at `approved` is a no-op.

Flow:

1. Look up the queue row by `proposal_id`.
2. Build a `Skill` from the row's fields (no `kind` / `scope` — Skill is
   flat) with `Provenance(council_session, validated_by, validated_at,
   evidence_chunks, adversary_critique, revision_count)`.
3. `SkillStore.save(skill)` writes `SKILL.md` under
   `<hierarchy_root>/<product>/<name>/SKILL.md`.
4. `commit_and_push()` commits + pushes (skill repo is a Git repo).
5. Embed the body as a doc chunk so the skill is itself retrievable.
6. Flip the queue row to `approved`.

## 7. MCP Server (`nexus/mcp_server/`)

Stdio MCP server launched by an MCP client (Claude Desktop, Cursor) as a
subprocess. One server instance per product:

```bash
uv run nexus-mcp-server --product <your-product-id>
```

### Tools

| Name | Purpose |
|---|---|
| `find_skills(query, context?, current_file?, top_k=5)` | Rank curated skills relevant to a query. Filters by `applies_to.files` (glob match against `current_file`) and `applies_to.contexts` (exact tag, `"general"` disables the filter). Write file globs as recursive repo-relative patterns such as `**/*.py`, `**/*.ts`, and `**/*.tsx` so Python 3.13 `PurePath.full_match()` and the older fallback keep the same intent. |
| `get_skill(name)` | Return the full body + frontmatter for a named skill. |
| `report_outcome(skill_name, succeeded, notes?)` | In-memory outcome log; surfaces in `state._outcomes`. |
| `query_code_context(symbol, file_glob?)` | Retrieval pipeline in `mode="code"`. |
| `hybrid_search_corpus(query, product_id?, top_k=5)` | Retrieval pipeline in `mode="auto"`. |

### Resources

- `nexus://meta-skill` — Jinja-rendered "how to use Nexus" doc.
- `nexus://hierarchy` — flat list of all skills for the active product.
- `nexus://skills/<name>` — markdown body for a named skill.
- `nexus://corpus/<product>` — counts (chunks, sources) for the product.

## 8. API Contracts (`nexus/api/routes/`)

FastAPI. CORS allows `http://localhost:3000`. All routes are listed below
with their backing logic.

### `/products`, `/me`

| Method + path | Purpose | Returns |
|---|---|---|
| `GET /me` | Static dev user + permission flags. | `{user, permissions}` |
| `GET /products` | List products with `lastCouncil`, skill counts. | `{products: Product[]}` |
| `GET /products/{id}` | Single product. | `Product` |
| `GET /products/{id}/status` | Drives dashboard card state. | `{hasEmbeddings, hasSkill, councilInProgress, currentSessionId, currentStage}` |
| `POST /products` | Create product. | `Product` |

Product onboarding in the UI creates product metadata plus a required GitHub
runtime source. The GitHub credential is a product service-account PAT stored
as encrypted source config, and `repos` may contain multiple GitHub HTTPS/SSH
URLs. Credentials are scoped per source; there is no product-level credential
bundle in v1.

`currentStage` precedence (highest wins): `review > skill > council >
ingesting > none`. `councilInProgress` is independent so the UI can render
"Run Council" vs "Council in progress" at the same stage.

### `/products/{id}/sources`

| Method + path | Purpose |
|---|---|
| `GET ""` | List config-defined + registry-defined sources. |
| `GET /{source_id}` | One source. |
| `POST ""` | Add a runtime source to the registry. |
| `DELETE /{source_id}` | Remove from registry. |
| `POST /{source_id}/sync` | Kick off ingest as a background task. Returns `{queued: true}` or `{already_running: true}`. |
| `GET /{source_id}/log` | SSE stream of JSON ingest events. Each event has `level`, `stage`, `msg`, `ts`, plus counters/URI/batch fields when relevant. |

GitHub sync validates all repo URLs before cloning, shallow-clones every repo
listed in `config.repos`, and ingests each clone under the same product. GitHub
resources use canonical `resource_uri` values like `github:owner/repo/path.py`;
temp clone directories never enter Qdrant or the manifest. A multi-repo source
gets one manifest `source_key` per repo, so resyncing one repo does not mark
the others removed. Sync stores aggregate `resourceCount` and emits one
combined repo map after successful ingest (warn on failure — the council still
runs without one).

Important SSE stages: `read`, `diff`, `skip`, `chunk`, `enrich`, `embed`,
`sparse`, `upsert`, `cleanup_stale`, `delete_removed`, `manifest_update`,
`enrichment_queue`, `complete`. Events include elapsed timings. The final
success message includes `added`, `updated`, `removed`, `unchanged`, `failed`,
and optional enrichment pending/failed counts.

Confluence and Jira are reserved for later source config screens, not product
onboarding. When added, Confluence must collect `base_url`, service-account
identity, token, and `space_keys`; Jira must collect equivalent Atlassian
credentials plus product-scope project keys.

### `/products/{id}/council/sessions` + `/council/sessions`

| Method + path | Purpose |
|---|---|
| `GET /products/{id}/council/sessions` | List sessions for a product. |
| `POST /products/{id}/council/sessions` | Body: `{topic: str}`. Schedules the skill-pack council as a background task. Returns `{session_id}`. |
| `GET /council/sessions/{sid}` | Persisted session row. |
| `GET /council/sessions/{sid}/stream` | SSE: live deliberation if running; deterministic replay if completed. |

### `/proposals`

| Method + path | Purpose |
|---|---|
| `GET ""` | List pending (or filtered) proposals. |
| `GET /{id}` | One proposal. |
| `POST /{id}/approve` | Calls `approve_proposal()`. |
| `POST /{id}/reject` | Body: `{reason, category}`. Persists rejection. |
| `POST /{id}/edit` | Body: `{body, actor}`. Persists edit (counts as a correction). |

### `/skills`

| Method + path | Purpose |
|---|---|
| `GET /products/{id}/skills` | Flat list of approved skills for a product. |
| `GET /skills/{skill_id}` | Full skill body + frontmatter. |
| `GET /skills/{skill_id}/corrections` | Critic notes from approved proposals + the built-in `provenance.adversary_critique`. |
| `GET /skills/{skill_id}/rejections` | Rejected proposals for this skill's product. |
| `GET /skills/{skill_id}/council-history` | Sessions for this skill's product. |

### `/setup`

| Method + path | Purpose |
|---|---|
| `GET /setup/status` | `{configured, skills_repo_url, source}`. |
| `POST /setup/skills-repo` | Body: `{mode: "create"|"existing", github_org?, repo_name?, existing_repo_url?}`. Mints or attaches the org-wide skills repo. |

### `/products/{id}/dashboard`

Aggregate snapshot for the dashboard screen:
`{daemon, pipeline, pending, recentActivity}`.

## 9. Configuration (`nexus.yaml`)

```yaml
skills_repo: git@github.com:org/nexus-skills.git
hierarchy_root: ./skills

connectors:
  - name: github
    type: github
    token: ${GITHUB_TOKEN}
    repos:
      - https://github.com/myorg/api
      - https://github.com/myorg/web

vector_store:
  url: http://localhost:6333
  collections:
    code: nexus_code
    text: nexus_text
  quantization:
    enabled: true
    type: turboquant              # Qdrant v1.18+ native TurboQuant
    bits: bits4                   # bits4 best recall; lower bits compress more
    always_ram: true

models:
  council:                     # default for drafter + critic + reviser
    provider: deepinfra
    model: google/gemma-4-26B-A4B-it
    api_key: ${DEEPINFRA_API_KEY}
    base_url: https://api.deepinfra.com/v1/openai
  # Optional role-specific overrides. Omit any role to use models.council.
  drafter:
    provider: deepinfra
    model: google/gemma-4-26B-A4B-it
    api_key: ${DEEPINFRA_API_KEY}
    base_url: https://api.deepinfra.com/v1/openai
  critic:
    provider: deepinfra
    model: google/gemma-4-26B-A4B-it
    api_key: ${DEEPINFRA_API_KEY}
    base_url: https://api.deepinfra.com/v1/openai
  reviser:
    provider: deepinfra
    model: google/gemma-4-26B-A4B-it
    api_key: ${DEEPINFRA_API_KEY}
    base_url: https://api.deepinfra.com/v1/openai
  light:                       # optional enricher (HQE + Anthropic CR)
    provider: deepinfra
    model: google/gemma-4-26B-A4B-it
    api_key: ${DEEPINFRA_API_KEY}
    base_url: https://api.deepinfra.com/v1/openai
  embedding:
    provider: deepinfra
    model: Qwen/Qwen3-Embedding-4B
    api_key: ${DEEPINFRA_API_KEY}
    base_url: https://api.deepinfra.com/v1/openai
    dim: 2560
    instruction_profile: qwen3
  reranker:
    provider: deepinfra
    model: Qwen/Qwen3-Reranker-4B
    api_key: ${DEEPINFRA_API_KEY}
    base_url: https://api.deepinfra.com/v1/inference

ingestion:
  enrich_chunks:
    docs: false                # optional Anthropic Contextual Retrieval
    code: false                # optional HQE
  embed_batch_size: 32
  quality_gate_threshold: 0.0  # reranker score gate; calibrate per provider via eval
  file_batch_size: 50
  read_concurrency: 10
  batch_concurrency: 2
  enricher_concurrency: 4
  enrichment_worker:
    enabled: false
    poll_interval_s: 5.0
    max_attempts: 3

server:
  host: 0.0.0.0
  port: 8000

storage:
  proposal_queue: ./data/proposals.db
  council_checkpoint: ./data/council.sqlite
```

`<state_dir>` is `storage.proposal_queue.parent` — also holds
`repomaps/<product_id>.json` and `registry.db`.

Changing embedding provider/model/dim/instruction profile or Qdrant
quantization settings forces resync through `embedding_version(config)`. Qdrant
collections must be recreated or explicitly updated if the embedding dimension
or quantization config changes after collection creation. Changing optional
enrichment settings only affects `enrichment_version(config)` and queues
background enrichment when enabled.

`${VAR}` substitution is performed at load time
(`nexus/config.py::_expand_env`).

## 10. Eval Strategy

Nexus has four eval surfaces:

| Surface | Runner | Scope | CI status |
|---|---|---|---|
| Unified eval harness | `nexus eval run --suite all` / `python -m evals.harness` | Runs suite defaults, optionally ingests fixtures, writes JSON + Markdown artifacts | CI uses retrieval on PRs; RAG/code on main/scheduled/manual when credentials exist |
| Retrieval quality | `pytest -m eval` / `python -m tests.eval.harness` | Production retrieval over `tests/eval/queries.json`; includes local, global, relational, and negative questions | Opt-in; skips when Qdrant/embedder/reranker are absent |
| RAGAS-style golden eval | `python -m evals.run_ragas` | Golden skill queries over `evals/golden.jsonl`; LLM-judged faithfulness and answer quality | CI via unified harness on main/scheduled/manual when `DEEPINFRA_API_KEY` is configured |
| Code retrieval eval | `python -m evals.run_code_eval` | Golden-set nDCG/recall and pairwise answer preference | CI via unified harness on main/scheduled/manual when `DEEPINFRA_API_KEY` is configured |

The preferred entrypoint is:

```bash
uv run nexus eval run --suite retrieval
uv run nexus eval run --suite rag,code --limit 10
```

Each run writes `artifacts/evals/<run_id>/summary.{json,md}` plus per-suite
JSON. Suite defaults ingest the Nexus repo for retrieval and the Forge seed
skills fixture for RAG/code unless `--no-ingest-fixture` is passed.

### Retrieval Eval (`tests/eval/`)

`tests/eval/queries.json` is the authoritative measure of retrieval
quality. It contains hand-curated queries against this codebase itself,
including exact symbol lookup, global architecture questions, graph/retrieval
questions, and negative capability questions. Each entry:

```json
{
  "query": "Anthropic contextual retrieval prompt for doc chunks",
  "expected": [{"file": "nexus/ingest/enricher.py"}],
  "tags": ["ingest", "enricher"]
}
```

A retrieved chunk matches an expected entry if its `resource_uri` ends with
the file path (and optionally overlaps a `line_start..line_end` range).

Two metrics, both reported by `EvalReport.render()`:

- **recall@K** — fraction of queries with ≥ 1 match in the top-K.
- **MRR** — mean reciprocal rank of the first match per query.

Floors live in `queries.json._meta`:
```
"min_recall_at_10": 0.6,
"min_mrr":          0.35
```

Both are conservative starting points. Bump as the evidence engine materially
improves. Run via:

```bash
pytest -m eval                                    # via pytest, skips if infra absent
uv run python -m tests.eval.harness --product <pid>  # standalone CLI
```

The CLI exits non-zero when either floor is violated — drops into CI
cleanly. Re-run after any change to chunking, optional enrichment, hybrid,
rerank, repo map, graph extraction, exact grep, or evidence assembly.

Qdrant with native TurboQuant is the only vector-index path. Eval compares the
current stack against the floors in `queries.json._meta`.

### RAGAS-Style Golden Eval (`evals/run_ragas.py`)

This runner uses `evals/golden.jsonl` skill questions. For each query, it:

1. Retrieves the top 8 contexts through the production retrieval pipeline.
2. Synthesizes a 2-4 sentence answer using only retrieved contexts.
3. Uses the configured council model as a strict JSON judge for faithfulness
   and answer relevancy.
4. Computes context recall by checking whether expected file names appear in
   retrieved hit URIs.

Aggregates are simple means over the evaluated items:

```
faithfulness      >= 0.85
answer_relevancy  >= 0.80
context_recall    >= 0.75
```

The runner is RAGAS-style, but does not import `ragas`; the in-house prompts
avoid dependency and prompt-template churn. In CI, `.github/workflows/ci.yml`
runs this as `ragas-regression` only when `DEEPINFRA_API_KEY` is configured:
Qdrant starts as a service, the seed Forge skills are ingested, the runner uses
`--limit 10`, and `evals/ci_ragas.json` is uploaded as an artifact. If
`evals/baseline_faithfulness.txt` exists, CI also fails when faithfulness drops
by more than `0.05` from that baseline.

### Code Retrieval Eval (`evals/run_code_eval.py`)

This manual runner uses the same golden set and retrieves top 10 contexts. It
reports:

- **nDCG@10** — ranking quality for expected files in the retrieved list.
- **Recall@10** — expected-file coverage in the top 10.
- **Pairwise preference accuracy** — for items with an `anti_answer`, the judge
  chooses between the expected answer and anti-answer using retrieved contexts.

Current gates:

```
nDCG@10                      >= 0.75
Recall@10                    >= 0.80
pairwise_preference_accuracy >= 0.85
```

This runner is not part of CI today; use it manually when changing code-focused
retrieval behavior or expanding `evals/golden.jsonl`.

## 11. Storage

- **Skills repo** — single Git repo, one per org, cloned to
  `hierarchy_root`. The first commit comes from the council's first
  approval; setup creates the repo empty.
- **Derived retrieval index** — Qdrant dense + sparse collections.
- **SQLite** — `proposals` + `sessions` (`storage.proposal_queue`);
  `registry.db` (products, users, runtime sources, sync manifests, sync runs,
  setup KV).
- **Local files** — `<state_dir>/repomaps/<product_id>.json` per product.

Product deletion is intentionally guarded behind the CLI:

```bash
uv run nexus delete-product --product <pid>        # dry-run
uv run nexus delete-product --product <pid> --yes  # delete
```

It removes product-scoped registry rows, source manifests/runs, proposals,
sessions, approved skill files, retrieval index entries, repo map, and
LangGraph checkpoints for that product's session IDs. `--skip-qdrant` skips
derived index cleanup for offline/local-only recovery.

## 12. Tech Stack

- **Python 3.13**, managed by `uv`. `from __future__ import annotations`
  in every module.
- **FastAPI** for the API. **Pydantic** for boundary types. **httpx** for
  outbound HTTP. **LangGraph** for the council state machine.
- **tree-sitter** for code parsing (Python, TS, TSX, JS, Rust, Go).
- **Qdrant** for vector + sparse storage, with native TurboQuant for dense
  vector compression. **fastembed** backs Qdrant BM25 sparse encoding.
- **DeepInfra Qwen3** embeddings/reranker are the default low-resource dev
  profile. Local **Jina v4** embeddings + **Jina Reranker v3** remain available
  via llama.cpp for high-resource/offline machines.
- **DeepInfra** (OpenAI-compatible) for council LLMs and optional enrichment in
  dev. Swap the `provider` + `base_url` to point at any compatible endpoint.
- **MCP** (stdio transport) for the agent-facing skill server.

## 13. Cut layers — kept out by design

The following were in earlier iterations and have been removed. Don't
reintroduce them without a written justification + a measured win on the
eval set.

- **Assistant layer** (Jira/Confluence conversational + action loop)
- **Neo4j / GraphRAG** (entity-relation graph + graph-expansion retrieval
  stage)
- **HyDE, query classifier, semantic cache, circuit breakers, prompt-
  injection guard** (retrieval over-engineering)
- **Org library** (`OrgSkill`, tech_stack / language / security kinds,
  ratification flow, change requests)
- **Skill composition** (`composes_with`, SkillKind master / product_domain,
  SkillScope product / org)
- **Multi-agent council** (Archaeologist + Domain Expert + Synthesizer +
  Adversary — collapsed to 3 nodes per Reflexion)
- **Change-gated cadence, weekly cap, override flag, corrections compaction**
  (premature; council fires when the user clicks the button)
- **Webhook automation, PR review agent, changelog agent** (demo task
  runners; not on the core path)
