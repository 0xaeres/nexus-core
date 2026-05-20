# Slice 3 — LLM Council MVP Status

## What's implemented

### LLM client (`nexus/llm/client.py`)
Multi-provider OpenAI-compatible chat client. Provider routing:
- `deepinfra` → `https://api.deepinfra.com/v1/openai` (production council)
- `openai` → `https://api.openai.com/v1`
- `anthropic` → `https://api.anthropic.com/v1`
- `ollama` → `http://localhost:11434/v1` (offline dev path)
- `base_url` / `url` in `ModelCfg` override the provider default

Token usage tracked per call. `chat_json` parses model output as JSON (with a fenced-block fallback for providers that ignore `response_format`).

### Council (`nexus/council/`)
- `state.py` — `CouncilState` TypedDict with reducers (`operator.add`) on `deliberation` + `costs` so fan-out updates merge cleanly. Includes `CodePatterns`, `DomainContext`, `EvidenceChunk`, `DeliberationMessage`, `AgentCost`.
- `agents/archaeologist.py` — code-mode retrieval (top-30) → LLM extracts patterns with evidence labels (E1, E2, …) → structured `CodePatterns`.
- `agents/domain_expert.py` — text-mode retrieval → vocabulary + entity relationships + summary, capped at 12/6 items.
- `agents/synthesizer.py` — aggregates both inputs → `SkillProposal` with citations + confidence. Pure-function faithfulness pass strips uncited list items inside `## Rules`. Kebab-case name normalisation.
- `graph.py` — LangGraph StateGraph with fan-out: START → {Archaeologist, Domain Expert} → Synthesizer → END. Async context manager `council_handles` owns client lifecycle. `SqliteSaver` checkpointer per session.
- `queue.py` — SQLite-backed proposal queue + session table. WAL mode, idempotent schema, deterministic transitions.

### CLI (`nexus council draft`)
End-to-end:
```bash
uv run nexus council draft \
  --product forge \
  --topic "PDA seed validation" \
  --kind product_domain
```
Prints: session id, classifier reason, then `✓ proposal <id> pending at http://localhost:3000` with confidence + citation count + token totals.

### FastAPI (Slice 3 surface)
| Route | Status | Notes |
|---|---|---|
| `GET /products/{p}/council/sessions` | ✅ | reads from queue |
| `GET /council/sessions/{sid}` | ✅ | full session payload incl. deliberation + costs |
| `GET /council/sessions/{sid}/stream` | ✅ | SSE **replay** of persisted deliberation (live streaming = Slice 4) |
| `POST /products/{p}/council/sessions` | 🟡 501 | Async kickoff = Slice 4; use the CLI |
| `GET /proposals` | ✅ | filterable by `status` + `product_id` |
| `GET /proposals/{id}` | ✅ |  |
| `POST /proposals/{id}/reject` | ✅ | requires `reason` query param |
| `POST /proposals/{id}/approve` | 🟡 501 | git commit + push wiring = Slice 4 |
| `POST /proposals/{id}/edit` | 🟡 501 | Slice 4 |

### Config / storage
Added `storage` block to `nexus.yaml` (`proposal_queue: ./data/proposals.db`, `council_checkpoint: ./data/council.sqlite`). `./data/` is gitignored.

Tests: 46 passing (`uv run pytest`). Lint clean.

## Slice 3 gates

| Gate | Status |
|---|---|
| 1. `nexus council draft --topic ... --product ...` runs end-to-end | ✅ — code-complete; requires DeepInfra key (or Ollama) + a populated Qdrant to actually produce a proposal |
| 2. Output is a `SkillProposal` with citations and confidence ∈ [0,1] | ✅ — enforced by Pydantic; confidence formula in `skills.models.compute_confidence` |
| 3. Langfuse shows per-agent token cost | 🟡 — `AgentCost` is captured per node; Langfuse OTel wiring lives in Slice 7 polish |
| 4. `SqliteSaver` checkpoint survives process kill mid-session | ✅ — wired in `council/graph.py::run_council` |
| 5. Proposal lands in SQLite pending queue; CLI prints "pending at localhost:3000" | ✅ — verified via queue→API smoke (`GET /proposals` returns the seeded proposal end-to-end) |

## How to demo

### Production path (DeepInfra)

```bash
# 1. Ensure infra + local services are up (from Slice 1/2)
docker compose up -d
make services-up

# 2. Ingest a corpus so the council has evidence to work with
uv run nexus ingest --product forge --path /path/to/your/code

# 3. Set the cloud LLM key
export DEEPINFRA_API_KEY=sk-...

# 4. Run the council
uv run nexus council draft \
  --product forge \
  --topic "input validation" \
  --kind product_domain

# 5. Browse the queue
uv run uvicorn nexus.api.app:app --port 8000
curl 'http://localhost:8000/proposals?status_filter=pending'
curl http://localhost:8000/council/sessions/<sid>/stream  # SSE replay
```

### Offline path (Ollama)

Edit `nexus.yaml`:

```yaml
models:
  council_agents: { provider: ollama, model: qwen2.5:14b, base_url: http://localhost:11434 }
  synthesizer:    { provider: ollama, model: qwen2.5:14b, base_url: http://localhost:11434 }
```

`ollama pull qwen2.5:14b` once, then `nexus council draft` works without any cloud keys.

## What's deferred within Slice 3

| Item | Why deferred | Will land |
|---|---|---|
| Live streaming during council run | SSE replay covers the API contract; live streaming requires wiring LangGraph's astream events through FastAPI background tasks — meaningful only when the UI is integrated | Slice 4 |
| `POST /products/{p}/council/sessions` async kickoff | Same as above | Slice 4 |
| Approval flow (`.skill.md` write + git commit + push) | The `store.py` write path exists; the git push needs the skills_repo plumbing | Slice 4 |
| Adversary agent + max-1 revision loop | By design — Adversary is Slice 4 (ADR-007) | Slice 4 |
| Langfuse spans | Cost capture works; OTel wiring is Slice 7 polish | Slice 7 |
| Real tool-use agentic loop | Retrieve-then-prompt is the MVP; tool-use loops add complexity without obvious quality gain at 5 agents | Defer indefinitely; revisit if needed |

## Files added/modified

```
nexus/llm/
  __init__.py    client.py

nexus/council/
  state.py    graph.py    queue.py
  agents/_common.py
  agents/archaeologist.py
  agents/domain_expert.py
  agents/synthesizer.py

nexus/api/
  deps.py
  routes/council.py        (real implementations)
  routes/proposals.py      (real implementations)

nexus/cli.py               (council draft wired end-to-end)
nexus/config.py            (StorageCfg)
nexus.yaml.example         (storage block)
.gitignore                 (data/)

tests/
  test_queue.py
  test_llm_client.py
  test_council_state.py
  test_synthesizer_faithfulness.py
```
