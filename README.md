<p align="center">
  <img src="./docs/assets/nexus-wordmark.svg" alt="Nexus" width="280">
</p>

<p align="center">
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-7C8CFF.svg"></a>
  <img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-12121A.svg">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-backend-009688.svg">
  <img alt="MCP-native" src="https://img.shields.io/badge/MCP-native-7C8CFF.svg">
  <img alt="LLM Council" src="https://img.shields.io/badge/LLM-council-12121A.svg">
  <img alt="RAG" src="https://img.shields.io/badge/RAG-dense%20%2B%20BM25%20%2B%20rerank-7C8CFF.svg">
</p>

# Nexus — Context Engine for Engineering Teams

Nexus is a **sovereign, MCP-native context engine** for your codebase. It
ingests your code and docs, runs a bounded expert LLM council to draft curated
product skill packs with human approval, and serves those skills back via MCP to any AI
client (Claude, Cursor, Continue, etc.).

Every AI tool your team uses gets grounded in *your* actual code and
conventions — not hallucinated from general training data.

```
Your codebase + docs
        │
        ▼
  Nexus ingests (GitHub or local filesystem)
        │
        ▼
  Hybrid retrieval (dense + BM25 + rerank)
        │
        ▼
  Expert council drafts a product skill pack
        │  ← seeded by an aider-style repo map + retrieved chunks
        ▼
  Human reviews + approves proposals in the UI
        │
        ▼
  Skills served via MCP to Claude / Cursor / Continue / any agent
```

---

## What is a skill file?

A skill is an Agent Skills directory with a `SKILL.md` file that tells an agent
*how to work in your codebase*: patterns to follow, pitfalls to avoid,
architectural context, domain vocabulary, and when to use the skill. One
product gets a fixed three-skill pack: context, architecture, and engineering.
Factual product claims cite file:line evidence; procedural playbook guidance
stays concise and uncited unless it names a concrete product fact.

```yaml
---
name: auth-token-rotation
description: Use for auth token rotation, refresh flows, session validation, and security review.
compatibility:
  agents: ["codex", "claude", "cursor", "continue"]
  format: agent-skills
metadata:
  nexus_product: my-api
  nexus_tier: interface
  nexus_confidence: 0.84
  nexus_provenance:
    council_session: cs_20260524_142117_a3f2c1
    validated_by: alice@example.com
    validated_at: 2026-05-24T14:25:00Z
    evidence_chunks: [c1, c2, c5]
    revision_count: 0
---

# auth-token-rotation

We rotate JWT tokens on every refresh; the prior token is short-lived (15m)
and the refresh token is rotated atomically with the access token.

## Rules
1. Always call `rotate_token()` — never mint a fresh token directly
   [file: src/auth/tokens.py:42].
2. Refresh and access tokens MUST rotate as a pair [file: src/auth/refresh.py:18].
3. Reject any token older than 15 minutes [file: src/auth/middleware.py:27].

## Anti-patterns
- Never store the refresh token in localStorage [file: src/auth/store.py:9].
- Don't share tokens across tenants — they're scoped per workspace.
```

---

## Local setup (Apple Silicon dev)

### Prerequisites

```bash
uv sync                               # installs everything from uv.lock

# One-time: local model servers
brew install llama.cpp
mkdir -p models
./scripts/download-models.sh          # Jina embedding v4 + reranker v3
```

### Configure

```bash
cp nexus.yaml.example nexus.yaml
cp .env.example .env
```

Edit `nexus.yaml`:
- `skills_repo` — the org's Git repo (optional; the first-run UI wizard at
  `/setup` can create one for you)
- `connectors` — optional static sources; most products are onboarded through
  the UI with a GitHub service-account token and one or more repo URLs

Edit `.env`:
- `DEEPINFRA_API_KEY` — council LLMs (and optional enrichment, if enabled)
- `GITHUB_TOKEN` — for the GitHub connector
- `NEXUS_TOKEN_KEY` — Fernet key for encrypting connector tokens at rest

### Start dev stack

```bash
make dev                              # Qdrant + API; DeepInfra does embeddings/rerank by default
```

If you use the optional local Jina profile, run `make local-models-up` to start
local llama.cpp embedder/reranker. `make dev` stays on Qdrant; dense vector
compression uses Qdrant's native TurboQuant by default.

```yaml
vector_store:
  quantization:
    enabled: true
    type: turboquant
    bits: bits4            # best recall; lower bits compress more
    always_ram: true
```

Model swaps need matching config. If you move from local Jina to a cloud
embedding/reranker, set the embedding dimension, instruction profile, and keep
`quality_gate_threshold: 0.0` until eval calibrates the new reranker scores.
Changing embedding provider/model/dimension/profile requires product resync.

Nexus does not currently understand visual Confluence architecture diagrams or
image attachments. It can index surrounding page text, but it does not extract
diagram boxes/arrows/labels, create visual embeddings, or cite image regions.

The local llama.cpp scripts auto-detect acceleration:

- Apple Silicon → Metal (`--n-gpu-layers 999`)
- Nvidia host → GPU
- otherwise → CPU

Local model defaults are conservative for a MacBook Air M2 with 8GB RAM. The
default DeepInfra ingest path uses larger client batches
(`embed_batch_size=32`, `file_batch_size=50`, `read_concurrency=10`,
`batch_concurrency=2`) because LLM chunk enrichment is off.

```bash
EMBEDDER_DEVICE=cpu RERANKER_DEVICE=cpu make services-up   # force CPU
EMBEDDER_UBATCH=2048 RERANKER_UBATCH=2048 make services-up # larger RAM
```

### Run the API

```bash
uv run uvicorn nexus.api.app:app --port 8000 --reload
# → http://localhost:8000/health  {"status":"ok"}
```

### Run the UI

```bash
cd ../nexus-ui
npm install
npm run dev
# → http://localhost:3000
```

On first boot there's no skills repository and no products. The app routes
you to `/setup` — a one-time wizard that either creates a new GitHub repo or
attaches to one you already own. The repo is initialised empty; skill files
land as the council approves them.

### Deploy: Oracle VM + Vercel

Full runbook: [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md).

Backend production shape is one Oracle VM running Docker Compose:

```bash
cp nexus.prod.yaml.example nexus.yaml
cp .env.example .env
# fill DEEPINFRA_API_KEY, NEXUS_TOKEN_KEY, NEXUS_SECRET_KEY,
# NEXUS_ADMIN_API_KEY, NEXUS_BOOTSTRAP_ADMIN_EMAIL/PASSWORD,
# NEXUS_ALLOWED_ORIGINS=https://<your-vercel-app>, and NEXUS_API_DOMAIN
docker compose -f docker-compose.prod.yml up -d --build
```

Only Caddy exposes `80/443`; Qdrant stays on the private Compose network. The
API is protected when `NEXUS_SECRET_KEY` is set. Browser sessions use secure
HttpOnly cookies plus CSRF headers, and the bootstrap admin is created from
env on first boot. Vercel needs:

```bash
NEXT_PUBLIC_NEXUS_API=https://<NEXUS_API_DOMAIN>
```

Langfuse tracing is enabled when `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` are set. Prompt/response capture is off by default via
`NEXUS_TRACE_CONTENT=false`.

---

## End-to-end flow

### 1. First-run setup (one-time, org-wide)

Visit `http://localhost:3000/setup`:
- **Create new repo** — Nexus uses `GITHUB_TOKEN` to mint a fresh repo.
- **Use existing repo** — paste a clone URL; Nexus verifies it can clone.

### 2. Onboard a product via the UI

- Create the product (`/new`)
- Provide the product service-account GitHub PAT and one or more GitHub repo
  URLs; Nexus creates the product-scoped GitHub source and starts ingest
- Trigger ingestion; watch the live SSE sync log. Resync is delta-safe:
  unchanged files are skipped, changed files are embedded before stale vectors
  are deleted, and removed files are cleaned from the configured retrieval
  index after successful delete-by-ID. Default ingest is fast raw
  dense+BM25 indexing; LLM chunk enrichment is disabled by default and can be
  re-enabled per config later.
- Start a council session; watch the live deliberation
- Approve / edit / reject the proposal at `/p/<id>/review`

### 3. CLI alternative

```bash
uv run nexus council draft \
  --product <your-product-id> \
  --topic "authentication middleware"

open http://localhost:3000/p/<your-product-id>/review
```

### 4. Delete a product completely

Use the guarded CLI cleanup when a test product needs to be removed from local
state and the derived retrieval index.

```bash
# Dry-run first. Shows every product-scoped thing that would be removed.
uv run nexus delete-product --product <your-product-id>

# Actually delete.
uv run nexus delete-product --product <your-product-id> --yes
```

This removes the product row, sources, source manifests, sync runs, proposals,
council sessions, approved `SKILL.md` files, retrieval index entries, the
persisted repo map, and LangGraph checkpoints for that product's sessions.

If the retrieval backend is offline and you only want local SQLite/filesystem cleanup:

```bash
uv run nexus delete-product --product <your-product-id> --yes --skip-qdrant
```

### 5. Use Nexus from Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/nexus",
        "run", "nexus-mcp-server",
        "--product", "<your-product-id>"
      ],
      "env": {
        "NEXUS_CONFIG": "/absolute/path/to/nexus/nexus.yaml"
      }
    }
  }
}
```

The MCP server exposes:
- `find_skills(query, context?, current_file?, top_k?)`
- `get_skill(name)`
- `report_outcome(skill_name, succeeded, notes?)`
- `query_code_context(symbol, file_glob?)`
- `hybrid_search_corpus(query, product_id?, top_k?)`

---

## Project layout

```
nexus/
├── nexus/
│   ├── api/           FastAPI routes (/products, /sources, /council, /skills, /setup)
│   ├── ingest/        Delta-safe manifest sync, chunker (tree-sitter),
│   │                  optional background enricher, embedder,
│   │                  Qdrant indexer (dense + BM25 + TurboQuant)
│   ├── retrieval/     Hybrid pipeline (dense + BM25 → RRF → reranker),
│   │                  repomap (aider-style symbol outline)
│   ├── council/       LangGraph skill-pack council: planner, experts, synthesizer
│   │                  Plus runner (SSE), queue (SQLite), skill_parser
│   ├── skills/        Skill model, Agent Skills store, approval flow
│   ├── connectors/    local_fs + MCP client
│   ├── mcp_server/    MCP stdio server — what Claude Desktop connects to
│   ├── llm/           OpenAI-compatible chat client (continuation-aware)
│   ├── daemon.py      Continuous index daemon
│   └── config.py      nexus.yaml loader
├── tests/             unit + integration tests
├── tests/eval/        41-query retrieval benchmark (recall@10 + MRR)
└── docker-compose.yml
```

---

## Quality gates

```bash
uv run ruff check nexus tests
uv run pytest -q                       # unit + integration tests
uv run pytest -m eval                  # opt-in retrieval benchmark
uv run python -m evals.run_ragas       # RAGAS-style golden eval
uv run python -m evals.run_code_eval   # manual code retrieval eval
make test-live-e2e                     # live Qdrant E2E
```

The eval set under `tests/eval/queries.json` is the authoritative measure of
retrieval quality. After any change to chunking, optional enrichment, hybrid,
rerank, or repo map, run `pytest -m eval` against a populated
index and confirm `recall@10` + `MRR` stay above the floors in
`queries.json._meta`.

The CI workflow always runs lint + `pytest -q`. A separate `ragas-regression`
job runs when `DEEPINFRA_API_KEY` is configured: it starts Qdrant, ingests the
seed Forge skills, runs `evals.run_ragas --limit 10`, gates faithfulness,
answer relevancy, and context recall, and uploads `evals/ci_ragas.json`. The
`evals.run_code_eval` runner is a manual golden-set check for nDCG@10,
Recall@10, and pairwise preference accuracy; it is not wired into CI.

TurboQuant is the only supported dense-vector compression mode. Eval decisions
compare the current Qdrant stack against the floors in `queries.json._meta`.

---

## Documentation

| File | What it covers |
|---|---|
| [`AGENTS.md`](./AGENTS.md) | Quick orientation — invariants, conventions, commit checks. |
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | New-contributor guide — code map, end-to-end traces, dev workflow. |
| [`ENGINEERING.md`](./ENGINEERING.md) | Full architecture spec + data model. |
| [`../nexus-ui/DESIGN.md`](../nexus-ui/DESIGN.md) | UI design system rules. |

---

## License

MIT — see [LICENSE](./LICENSE).
