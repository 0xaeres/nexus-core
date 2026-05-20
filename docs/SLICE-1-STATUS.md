# Slice 1 — Ingestion Status

## What's implemented

Code-level deliverables for ENGINEERING.md §4 (ingestion pipeline):

| Module | Status | Notes |
|---|---|---|
| `nexus/ingest/models.py` | ✅ | `ResourceRef`, `Chunk`, `EmbeddedChunk`; deterministic UUID5 chunk IDs |
| `nexus/ingest/chunker.py` | ✅ | Tree-sitter for `.py`/`.ts`/`.tsx`/`.js`/`.jsx`/`.rs`/`.go`; heading splitter for `.md`/`.mdx`; char-split fallback. `file:line` anchors. |
| `nexus/ingest/enricher.py` | ✅ | Ollama HTTP client; per-source-type toggle (ADR-010). |
| `nexus/ingest/embedder.py` | ✅ | Jina v4 via llama-server `/v1/embeddings` with task-mode instruction prefixes (`dense_code` / `dense_text`). |
| `nexus/ingest/indexer.py` | ✅ | Two Qdrant collections with `sharding_method: custom` keyed on `product_id` (§12 isolation). |
| `nexus/ingest/pipeline.py` | ✅ | End-to-end orchestrator: source → chunk → enrich → embed → index. |
| `nexus/connectors/local_fs.py` | ✅ | Stand-in source for Slice 1 demo (real MCP client deferred). |
| `nexus/cli.py` | ✅ | `nexus ingest --product P --path PATH`; `nexus query "text" -p P`. |

Tests passing (`uv run pytest`): 7/7. Lint clean (`uv run ruff check nexus tests`).

## What's deferred within Slice 1

| Item | Why deferred | Will land |
|---|---|---|
| MCP client (`connectors/mcp_client.py`) + Manager | Local-fs source covers the demo end-to-end. Real MCP servers (GitHub etc.) need network setup orthogonal to the data plane. | Within-Slice-1 follow-on |
| `nexus init` interactive wizard | Trivial scaffolding; `cp nexus.yaml.example nexus.yaml` works today | Within-Slice-1 follow-on |
| Sparse BM25 vectors | Slice 2 (retrieval pipeline) — Stage 2 of the 5-stage GraphRAG flow | Slice 2 |
| Docling for PDFs | Heavy dep; markdown + code covers the demo corpus | When PDF source appears |

## Slice 1 gates (per plan)

| Gate | Status |
|---|---|
| 1. `nexus init` writes a valid `nexus.yaml` from prompts | ⏳ — falls back to `cp nexus.yaml.example` for now |
| 2. MCP Connector Manager runs `resources/list` + `resources/read` against a real GitHub MCP server | ⏳ — local-fs source proves the same data-plane contract |
| 3. Chunks carry `file:line` anchors after tree-sitter | ✅ — verified in `tests/test_chunker.py` |
| 4. `nexus query "swap authority check"` returns top-k chunks with file:line (dense-only) | ✅ in code — requires services to demo |
| 5. Qdrant shows `product_id`-shard-keyed vectors | ✅ in code — verified by inspection; requires running Qdrant to demo |

## How to demo end-to-end

Prereqs (one-time):

```bash
brew install llama.cpp ollama
mkdir -p models
# Download a Jina v4 GGUF into models/, e.g.:
#   huggingface-cli download <jinaai-org>/jina-embeddings-v4-GGUF \
#       jina-embeddings-v4.Q4_K_M.gguf --local-dir models/
```

Then:

```bash
# 1. Infra + local model services
docker compose up -d
make services-up                 # llama.cpp embedder + reranker + ollama qwen2.5:3b

# 2. Config
cp nexus.yaml.example nexus.yaml
cp .env.example .env             # fill DEEPINFRA_API_KEY etc.

# 3. Ingest a real codebase (this repo, for instance)
uv run nexus ingest --product forge --path /Users/aeres/Desktop/projects/nexus

# 4. Query
uv run nexus query "qdrant shard key" --product forge -k 5
```

Expected: a ranked list with file:line anchors pointing into `nexus/ingest/indexer.py`.

## Files added this slice

```
nexus/ingest/
  ├── models.py        # ResourceRef, Chunk, EmbeddedChunk
  ├── chunker.py       # tree-sitter + markdown splitter
  ├── enricher.py      # contextual prepend via Ollama
  ├── embedder.py      # Jina v4 client (llama.cpp HTTP)
  ├── indexer.py       # Qdrant upsert + search (shard-keyed)
  └── pipeline.py      # end-to-end orchestrator

nexus/connectors/
  └── local_fs.py      # MCP stand-in source

tests/
  ├── test_chunker.py
  └── test_local_fs.py
```
