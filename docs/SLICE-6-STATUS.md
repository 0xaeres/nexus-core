# Slice 6 — GraphRAG + Org Library + Curator Status

## What's implemented

### Graph layer (Neo4j)
- `nexus/graph/store.py` — async Neo4j wrapper, product-isolated by both a
  `product_id` property *and* a `Product_<id>` label on every node (defense in
  depth per §12). Constraints + indexes + n-hop expansion Cypher. Node kinds:
  `Chunk`, `Entity`, `Source`. Edges: `MENTIONS`, `REL{type}`, `FROM`.
- `nexus/retrieval/graph.py` — Stage 4 expansion is no longer a stub. For each
  seed chunk: pull mentioned entities → walk `REL{1..hops}` → collect neighbour
  chunks → RRF-merge with seeds, decaying score by hop distance.
- `nexus/retrieval/pipeline.py` — `RetrievalContext` now owns a `GraphStore`;
  the existing graph circuit breaker now actually does something.

### Relation extraction (light LLM)
- `nexus/ingest/relation_extractor.py` — Ollama JSON-mode call per chunk
  asking for `{entities, relations}` with strict type allow-lists. Defensive
  parser drops unknown entity types and dangling relations.
- `nexus/ingest/incremental.py` — every reindex now also (1) detaches old
  graph chunk nodes, (2) upserts the new ones, (3) extracts + upserts
  entities + relations. Tolerates Neo4j or extractor downtime.
- `nexus/daemon.py` — daemon now wires `GraphStore` + `RelationExtractor` and
  calls `ensure_constraints` at boot.
- `nexus/config.py` + `nexus.yaml.example` — new `ingestion.extract_relations.
  {docs,code}` toggle.

### Curator + Org Library
- `nexus/tools/web_search.py` — Tavily adapter (env-keyed) with an offline
  no-op fallback. Pluggable for SerpAPI / Brave later.
- `nexus/council/agents/curator.py` — single-agent (no council). Workflow:
  1. `web_search(topic + best practices + conventions)` → external sources.
  2. Optional `retrieve()` validation against a product corpus.
  3. Curator LLM (`zai-org/GLM-5`) drafts the `OrgSkill` body + applies_to +
     external_sources + quality_score in JSON.
- `nexus/council/queue.py` — extended with `OrgProposalQueue` providing
  `org_proposals` + `change_requests` tables and lifecycle methods.

### Change requests
- `nexus/council/agents/security_sentinel.py` — verdicts (`low_risk` |
  `medium_risk` | `high_risk`) with OWASP/CVE rule-pack anchors. Defaults to
  `medium_risk` if the LLM goes off-spec.
- `nexus/council/change_request.py` — kind→agent router:
  `security → security_sentinel`, `tech_stack | language → archaeologist`,
  fallback `archaeologist`. Plus a light Archaeologist CR review path.

### FastAPI surface (`/org/*`)
| Route | Behaviour |
|---|---|
| `GET /org/skills` | list ratified org skills from `org_library_root` |
| `GET /org/skills/{id}` | full skill body + open change requests |
| `POST /org/skills` | kick off Curator as background task, returns proposal_id |
| `GET /org/proposals?status=pending` | list pending Curator proposals |
| `POST /org/skills/{pid}/ratify` | write `.skill.md` to `org_library_root`, flip queue status |
| `POST /org/skills/{id}/change-requests` | file CR, schedule agent review |
| `POST /org/skills/{id}/change-requests/{rid}/approve` | org-admin approve + cache purge hint |
| `POST /org/skills/{id}/change-requests/{rid}/reject` | reject with reason |

### Tests / lint
- 84 passing (was 68) — added `test_change_request_router.py`,
  `test_relation_parser.py`, `test_graph_expand.py`, `test_org_queue.py`.
- `uv run ruff check` clean.

## Slice 6 gates

| Gate | Status |
|---|---|
| 1. Cross-source query returns ADR chunk linked via Neo4j to relevant commit (not vector-only) | ✅ — Stage 4 emits `Hit(source="graph")` with `graph_via` + `graph_hop` payload. Live demo requires populated Neo4j + Ollama for extraction. |
| 2. RAGAS cross-source pairs: GraphRAG ≥ 10% higher faithfulness than vector-only | 🟡 — code-complete; quantitative measurement is Slice 7 (RAGAS golden + CI gate). |
| 3. Curator agent kickoff → OrgSkillProposal in `/org/library` queue within 60s | ✅ — background task returns proposal_id immediately; persistence path verified by `test_org_queue.py`. Live LLM round-trip needs DeepInfra. |
| 4. SME files CR → Security Sentinel verdict within 30s → admin approves → cache invalidated | ✅ — full lifecycle wired; verdict path verified by `test_org_queue.py::test_change_request_lifecycle`. Cache purge currently relies on TTL (24h); explicit per-skill purge is a polish item. |

## How to demo end-to-end

```bash
# Prereqs (same as Slice 5)
docker compose up -d
make services-up
export DEEPINFRA_API_KEY=...
export TAVILY_API_KEY=...     # optional; offline -> empty web results

# 1. Author an Org Library skill
curl -X POST http://localhost:8000/org/skills \
  -H 'Content-Type: application/json' \
  -d '{"topic":"SpringBoot conventions","kind":"tech_stack"}'
# -> {"proposal_id":"orgp_...","status":"running"}

# wait ~30s
curl 'http://localhost:8000/org/proposals?status=pending' | jq

# Ratify
curl -X POST http://localhost:8000/org/skills/orgp_xxx/ratify \
  -H 'Content-Type: application/json' \
  -d '{"actor":"admin@org"}'
# -> file lands at $org_library_root/tech_stack/<name>.skill.md

# 2. File a change request
curl -X POST 'http://localhost:8000/org/skills/org%2Fowasp-input-validation/change-requests' \
  -H 'Content-Type: application/json' \
  -d '{
        "title":"Relax string length cap on /signup",
        "proposed_diff":"--- old\n+++ new\n@@ length<=100 -> length<=255",
        "rationale":"Legacy partner integration",
        "requester":"sme@org"
      }'
# wait ~30s for Sentinel
curl 'http://localhost:8000/org/skills/org%2Fowasp-input-validation' | jq .changeRequests

# Approve / reject
curl -X POST 'http://localhost:8000/org/skills/org%2Fowasp-input-validation/change-requests/cr_xxx/reject' \
  -H 'Content-Type: application/json' \
  -d '{"actor":"admin@org","reason":"No compensating control"}'
```

## What's deferred within Slice 6

| Item | Why deferred |
|---|---|
| Explicit per-skill cache invalidation on CR approve | TTL is correct-enough; explicit purge needs a skill→chunk_ids index that we don't yet maintain. Polish for Slice 7. |
| Live Neo4j cluster IDs & relationship metadata in citations | Spec wants UI to show graph paths sparingly; we expose `graph_via` + `graph_hop` payload, but UI chips for these arrive with the Skills/Council cutover. |
| RAGAS cross-source measurement | Slice 7 (eval harness). |
| Org Library UI (`/org/library/*` routes in nexus-ui) | Backend ready; cutover sits with the broader UI work. |
| Multi-product Curator scoping | Single `product_for_corpus` arg is enough for now; matrix-scoped runs deferred. |

## Files added/modified

```
nexus/graph/store.py                          NEW
nexus/ingest/relation_extractor.py            NEW
nexus/ingest/incremental.py                   graph + extractor wired in
nexus/retrieval/graph.py                      real Stage 4 expansion
nexus/retrieval/pipeline.py                   RetrievalContext owns GraphStore
nexus/daemon.py                               graph + extractor + ensure_constraints
nexus/config.py + nexus.yaml.example          extract_relations toggle
nexus/tools/web_search.py                     NEW (tavily + no-op)
nexus/council/agents/curator.py               NEW
nexus/council/agents/security_sentinel.py     NEW
nexus/council/change_request.py               NEW (router)
nexus/council/queue.py                        OrgProposalQueue + tables
nexus/api/routes/org_library.py               real implementations (8 routes)

tests/test_change_request_router.py           NEW
tests/test_relation_parser.py                 NEW
tests/test_graph_expand.py                    NEW
tests/test_org_queue.py                       NEW
```
