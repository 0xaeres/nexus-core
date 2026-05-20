# Nexus

Sovereign, MCP-native skill server for your codebase. Ingests code + docs through MCP connectors, runs an LLM Council to draft validated skills, and serves them back via MCP to any AI client.

Full spec: [ENGINEERING.md](./ENGINEERING.md). Per-slice status notes: [`docs/`](./docs).

## 15-minute quickstart (Apple Silicon dev)

```bash
# 1. Clone + Python deps
git clone <this-repo> && cd nexus
uv sync

# 2. Local LLM serving prereqs (one-time)
brew install llama.cpp ollama
mkdir -p models
# Download GGUFs into models/:
#   jina-embeddings-v4.Q4_K_M.gguf
#   jina-reranker-v3.Q4_K_M.gguf

# 3. Configure
cp nexus.yaml.example nexus.yaml
cp .env.example .env
# Edit .env: DEEPINFRA_API_KEY, GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET, NEO4J_PASSWORD

# 4. Infrastructure (Qdrant + Neo4j + Langfuse + Postgres)
docker compose up -d

# 5. Local model services (llama.cpp embedder + reranker + Ollama)
make services-up

# 6. Run the API
uv run uvicorn nexus.api.app:app --port 8000 --reload

# 7. Smoke
curl localhost:8000/health
# -> {"status":"ok"}
```

## End-to-end flows

### Ingest a codebase
```bash
uv run nexus ingest --product forge --path /path/to/your/repo
```

### Draft a skill via Council
```bash
uv run nexus council draft --product forge --topic "PDA seed validation" --kind product_domain
# -> proposal pending at http://localhost:3000
```

### Approve from the UI
```bash
cd ../nexus-ui && npm run dev
# Visit http://localhost:3000/p/forge/proposals
```

### Use Nexus from Claude Desktop
Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "nexus": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/nexus",
        "run", "nexus-mcp-server",
        "--product", "forge"
      ]
    }
  }
}
```

## Production deployment

The full stack runs from one Docker Compose with a profile:

```bash
docker compose --profile full up -d
# Brings up Qdrant, Neo4j, Langfuse, Postgres, AND nexus-api
```

llama.cpp services still run on the host on Apple Silicon (Metal). For Linux + NVIDIA, point `models.embedding.url` / `models.reranker.url` at any OpenAI-compatible embedding/reranker server.

## Quality gates

```bash
# Unit + integration tests
uv run pytest

# RAGAS-style retrieval + generation eval
uv run python -m evals.run_ragas --golden evals/golden.jsonl

# Code retrieval (nDCG@10, Recall@10, pairwise preference)
uv run python -m evals.run_code_eval --golden evals/golden.jsonl

# Resilience smoke (degraded modes)
bash scripts/resilience-smoke.sh
```

Gate thresholds:
- `faithfulness >= 0.85`, `answer_relevancy >= 0.80`, `context_recall >= 0.75`
- `nDCG@10 >= 0.75`, `Recall@10 >= 0.80`, pairwise preference `>= 0.85`

CI: `.github/workflows/ci.yml` runs lint + tests + RAGAS regression on every PR and fails if faithfulness drops > 5% from the baseline.

## Documentation

| File | Purpose |
|---|---|
| `ENGINEERING.md` | Full spec - architecture, data model, ADRs |
| `INTEGRATION.md` | UI <-> backend cutover map |
| `docs/SLICE-*-STATUS.md` | Per-slice delivery status |

## Slice progress

- [x] Slice 0 - Foundations (project, Docker, scripts)
- [x] Slice 1 - Ingestion via MCP
- [x] Slice 2 - 5-stage retrieval + MCP server
- [x] Slice 3 - LLM Council MVP (3 agents)
- [x] Slice 4 - Adversary + approval + async kickoff
- [x] Slice 5 - Continuous daemon + PR review + Changelog
- [x] Slice 6 - GraphRAG + Org Library + Curator
- [x] Slice 7 - Evals + CI + polish

## License

Proprietary - internal use only for now.
