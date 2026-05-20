# Slice 2 — Retrieval Pipeline + MCP Server Status

## What's implemented

### Retrieval — full 5-stage pipeline (ENGINEERING.md §5)

| Module | Status | Notes |
|---|---|---|
| `nexus/retrieval/sparse.py` | ✅ | fastembed Qdrant/bm25 sparse encoder (TF; Qdrant applies IDF server-side) |
| `nexus/retrieval/classifier.py` | ✅ | Heuristic simple-vs-complex with confidence (no LLM round-trip needed for the hot path) |
| `nexus/retrieval/hyde.py` | ✅ | Ollama client; mode-aware (code vs text) hypothetical generation |
| `nexus/retrieval/hybrid.py` | ✅ | Reciprocal Rank Fusion (k=60); rank-only, no score-scale dependency |
| `nexus/retrieval/graph.py` | 🟡 Stub | Stage 4 passes input through; real Neo4j 1-2 hop lands in Slice 6 |
| `nexus/retrieval/reranker.py` | ✅ | llama-server `/reranking` (Jina v3 GGUF) |
| `nexus/retrieval/cache.py` | ✅ | Semantic cache @ 0.92 cosine; per-product scope; chunk-id-based invalidation |
| `nexus/retrieval/circuit.py` | ✅ | Per-component breaker; 3 failures → open 30s → half-open probe |
| `nexus/retrieval/pipeline.py` | ✅ | Orchestrator with quality gate + re-query + degradation chain |
| `nexus/ingest/indexer.py` | ✅ | Extended to named-vectors `{dense, bm25}` with IDF modifier |
| `nexus/ingest/pipeline.py` | ✅ | Sparse encoding wired into ingest |

### Skills (ENGINEERING.md §7, §12)

| Module | Status | Notes |
|---|---|---|
| `nexus/skills/models.py` | ✅ | Skill, OrgSkill, SkillProposal, Provenance, Citation, Critique |
| `nexus/skills/store.py` | ✅ | Read/write `.skill.md` with YAML frontmatter; ISO-8601 datetime coercion |
| `nexus/skills/seed/` | ✅ | 5 hand-crafted golden-set anchors: master, pda-seed-validation, swap-fee-math, owasp-input-validation, typescript-conventions |

Git operations (clone-on-boot, push-on-approve) are deferred to Slice 4 with the approval flow.

### MCP server (ENGINEERING.md §8)

| Module | Status | Notes |
|---|---|---|
| `nexus/mcp_server/server.py` | ✅ | stdio transport; argparse for `--product`/`--config`; entry point `nexus-mcp-server` |
| `nexus/mcp_server/tools.py` | ✅ | All 5 tools: `find_skills`, `get_skill`, `report_outcome`, `query_code_context`, `hybrid_search_corpus` |
| `nexus/mcp_server/meta_skill.md.j2` | ✅ | Jinja template rendered at `nexus://meta-skill` |
| Resources | ✅ | `nexus://meta-skill`, `nexus://hierarchy`, `nexus://skills/{name}`, `nexus://corpus/{product}` |

Tests: 26 passing (`uv run pytest`). Lint clean (`uv run ruff check`).

## Slice 2 gates

| Gate | Status |
|---|---|
| 1. 5-stage retrieval outperforms vector-only on a smoke set | ⏳ — code-complete; quantitative eval lands with RAGAS in Slice 7 |
| 2. Semantic cache: second identical query returns < 50ms; Langfuse logs cache-hit span | ⏳ — code path verified; requires running Qdrant + Langfuse to demo |
| 3. `Skill.load()` / `Skill.save()` round-trip preserves frontmatter | ✅ — verified in `tests/test_skills_store.py::test_save_then_load_round_trip` |
| 4. Claude Desktop connects via stdio, reads `nexus://meta-skill`, calls `find_skills`, gets reranked results with citations | 🟡 — tool layer verified; Claude Desktop config snippet below; full demo requires GGUFs + Qdrant |
| 5. Resilience smoke: Neo4j down → degraded mode; Qdrant down → 503 | ✅ — verified in `tests/test_circuit.py` (state machine); chain wired in `retrieval/pipeline.py` |

## How to plug into Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "uv",
      "args": [
        "--directory", "/Users/aeres/Desktop/projects/nexus",
        "run", "nexus-mcp-server",
        "--product", "forge",
        "--config", "nexus.yaml"
      ]
    }
  }
}
```

Then in Claude Desktop, ask: *"How should I validate PDA seeds?"* — Claude should call `find_skills`, then `get_skill("pda-seed-validation")`, and ground its answer in the seed skill.

## Verified MCP tool flow (no external services)

The seed skills + tool layer were exercised end-to-end against `nexus.yaml` pointed at `./nexus/skills/seed`:

```
hierarchy: 5 skills
  - forge                          master             conf=0.82
  - pda-seed-validation            product_domain     conf=0.87
  - swap-fee-math                  product_domain     conf=0.79
  - owasp-input-validation         security           conf=0.91
  - typescript-conventions         language           conf=0.88

find_skills("pda bump seed validation"):
  1.17  pda-seed-validation
  1.16  forge
  0.68  owasp-input-validation
```

## What's deferred within Slice 2

| Item | Why deferred | Will land |
|---|---|---|
| Real MCP client (`connectors/mcp_client.py`) for ingestion | local-fs source still covers the ingestion data plane | Slice 5 (daemon needs it) |
| Git-backed skills store (clone/push) | Read/write works; git ops needed only when approve flow exists | Slice 4 |
| Stage 4 Neo4j expansion | Relation extractor isn't built yet | Slice 6 |
| Langfuse OTel spans for retrieval stages | Code paths exist; instrumenting takes the spec's per-stage table and wires it into the orchestrator | Slice 7 polish |
| Lexical-only `find_skills` ranker | Works well enough for 5 seed skills; semantic ranker is a Slice 4 polish | Slice 4 |

## Files added/modified this slice

```
nexus/retrieval/
  sparse.py     classifier.py    hyde.py      reranker.py
  cache.py      circuit.py       hybrid.py    graph.py
  pipeline.py

nexus/skills/
  models.py     store.py
  seed/master.skill.md
  seed/L2_domain/pda-seed-validation.skill.md
  seed/L2_domain/swap-fee-math.skill.md
  seed/org/owasp-input-validation.skill.md
  seed/org/typescript-conventions.skill.md

nexus/mcp_server/
  server.py     tools.py     meta_skill.md.j2

nexus/ingest/indexer.py     (named vectors {dense, bm25})
nexus/ingest/pipeline.py    (sparse encoding hook)
nexus/cli.py                (query → 5-stage pipeline)

tests/
  test_rrf.py  test_circuit.py  test_classifier.py  test_skills_store.py
```
