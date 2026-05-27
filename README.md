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
  Hybrid retrieval (dense + BM25 + Jina rerank)
        │
        ▼
  Expert council drafts a product skill pack
        │  ← seeded by an aider-style repo map + contextual chunks
        ▼
  Human reviews + approves proposals in the UI
        │
        ▼
  Skills served via MCP to Claude / Cursor / Continue / any agent
```

---

## What is a skill file?

A skill file is a plain Markdown + YAML document that tells an agent *how to
work in your codebase*: patterns to follow, pitfalls to avoid, architectural
context, domain vocabulary. One product → one or more skills, each with
file:line citations back to the source.

```yaml
---
name: auth-token-rotation
product: my-api
version: 1
confidence: 0.84
applies_to:
  files: ["src/auth/**/*.py"]
  contexts: ["code-review", "security-audit"]
provenance:
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
- `DEEPINFRA_API_KEY` — council + enricher LLMs (get one at deepinfra.com)
- `GITHUB_TOKEN` — for the GitHub connector
- `NEXUS_TOKEN_KEY` — Fernet key for encrypting connector tokens at rest

### Start dev stack

```bash
make dev                              # Qdrant + llama.cpp embedder/reranker + API
```

If you only want the backing services without the API, use `make services-up`.

The local llama.cpp scripts auto-detect acceleration:

- Apple Silicon → Metal (`--n-gpu-layers 999`)
- Nvidia host → GPU
- otherwise → CPU

Defaults are conservative for a MacBook Air M2 with 8GB RAM:

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
  are deleted, and removed files are cleaned from Qdrant after successful
  delete-by-ID.
- Start a council session; watch the live deliberation
- Approve / edit / reject the proposal at `/p/<id>/review`

### 3. CLI alternative

```bash
uv run nexus council draft \
  --product <your-product-id> \
  --topic "authentication middleware"

open http://localhost:3000/p/<your-product-id>/review
```

### 4. Use Nexus from Claude Desktop

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
│   │                  enricher (HQE + Anthropic CR), embedder (Jina v4),
│   │                  indexer (Qdrant dense + BM25)
│   ├── retrieval/     Hybrid pipeline (dense + BM25 → RRF → Jina reranker),
│   │                  repomap (aider-style symbol outline)
│   ├── council/       LangGraph skill-pack council: planner, experts, synthesizer
│   │                  Plus runner (SSE), queue (SQLite), skill_parser
│   ├── skills/        Skill model, store (YAML+Markdown), approval flow
│   ├── connectors/    local_fs + MCP client
│   ├── mcp_server/    MCP stdio server — what Claude Desktop connects to
│   ├── llm/           OpenAI-compatible chat client (continuation-aware)
│   ├── daemon.py      Continuous index daemon
│   └── config.py      nexus.yaml loader
├── tests/             unit + integration tests
├── tests/eval/        40-query retrieval benchmark (recall@10 + MRR)
└── docker-compose.yml
```

---

## Quality gates

```bash
uv run ruff check nexus tests
uv run pytest -q                       # 146 tests, ~2s
uv run pytest -m eval                  # opt-in retrieval benchmark
```

The eval set under `tests/eval/queries.json` is the authoritative measure of
retrieval quality. After any change to chunking, enrichment, hybrid, rerank,
repo map, or contextual retrieval, run `pytest -m eval` against a populated
index and confirm `recall@10` + `MRR` stay above the floors in
`queries.json._meta`.

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

Proprietary — internal use only.
