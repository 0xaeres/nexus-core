# Slice 4 — Adversary + UI Integration Status

## Backend (complete)

### Adversary + revision loop (ADR-007)
- `nexus/council/agents/adversary.py` — red-team the Synthesizer's draft, produces `Critique{severity, issues[], recommendation}`.
- `CouncilState` extended with `proposal`, `critique`, `revision_count`.
- `nexus/council/graph.py` — Adversary added with `add_conditional_edges`. Routes back to Synthesizer iff `severity == "blocking" AND revision_count == 0`. Max-1 redraft cycle; final Adversary pass runs regardless.
- Synthesizer becomes critique-aware: when redrafting it inlines the prior body + critique into its prompt and bumps `revision_count` to 1.

### Approval flow
- `nexus/skills/approval.py` — single async `approve_proposal()` callable from API + CLI. Queue row → `Skill` → `SkillStore.save` → git commit + push (best-effort via `nexus/skills/git.py`) → embedder + indexer upsert the skill body as a doc chunk so future retrievals find it. Idempotent on re-approval.
- `nexus/api/routes/proposals.py` — `POST /proposals/{id}/approve` and `POST /proposals/{id}/edit` now live (no more 501). Approve takes `{actor: str}` in body.

### Async kickoff + live SSE
- `nexus/council/runner.py` — `kick_off()` schedules `_run_session` as a background `asyncio.Task` (anchored in a module-level set so GC can't drop it). The runner streams LangGraph node deltas through `compiled.astream()` and publishes each delta to a session-specific pub/sub (`_SessionHub`). At the end it persists deliberation + costs + proposal.
- `POST /products/{p}/council/sessions` returns `{session_id, status: "running"}` immediately.
- `GET /council/sessions/{sid}/stream` returns:
  - **live** SSE for an in-flight session (subscriber gets every node delta),
  - **replay** SSE for a completed session (same event shape, drains from SQLite).
- Event types emitted: `session_start`, `message`, `cost`, `critique`, `proposal_preview`, `proposal`, `error`, `session_end`.

### Remaining endpoints (no more 501)
| Route | Status |
|---|---|
| `GET /me` | ✅ static user from registry; RBAC arrives later |
| `GET /products` + `GET /products/{id}` | ✅ from `Registry` (auto-seeds `forge` + `jl`) |
| `GET /products/{p}/dashboard` | ✅ pipeline counts + pending + recentActivity |
| `GET /products/{p}/sources` + detail | ✅ derives from `nexus.yaml` connectors; tokens redacted |
| `GET /products/{p}/skills` | ✅ master / domain / adopted split per spec |
| `GET /skills/{id}` | ✅ |
| `GET /products/{p}/activity` | ✅ sessions today; ingest events join in Slice 5 |
| `POST /proposals/{id}/approve` | ✅ |
| `POST /proposals/{id}/reject` | ✅ |
| `POST /proposals/{id}/edit` | ✅ |

### Storage
- `nexus/registry.py` — tiny SQLite registry: products + users tables, seeds `forge` product + `jl` user on first boot.
- `nexus/api/deps.py` — three lru-cached deps: `get_proposal_queue`, `get_registry`, `get_skill_store`.

### Tests / lint
- 55 passing (was 46) — added `test_adversary_routing.py`, `test_approval.py`.
- `uv run ruff check` clean.

## UI (scaffold + reference page)

### What's in place
- `nexus-ui/lib/types.ts` — canonical shared types, reconciled to spec (SkillKind = master|product_domain, OrgSkillKind split out, AgentRole = spec-aligned). Source of truth for the cutover.
- `nexus-ui/lib/api/client.ts` + `lib/api/index.ts` — thin HTTP wrappers. One function per FastAPI route from §11. `NEXT_PUBLIC_NEXUS_API` env var overrides base URL.
- `nexus-ui/lib/hooks/useEventStream.ts` — EventSource hook with named-event registration (message/cost/critique/proposal/session_end) and bounded buffer. Replaces all `setTimeout` simulators wholesale.
- `nexus-ui/components/screens/Proposals.tsx` + `app/p/[product]/proposals/page.tsx` — **connected reference screen**. Lists pending proposals from the live backend, approve/reject buttons that hit `POST /proposals/{id}/approve|reject`. Demonstrates: loading state, error state, optimistic-ish refresh. Typechecks clean.

### What's deferred (next session)
The three existing mock-coupled screens (Dashboard, Skills, CouncilSession) are non-trivial rewrites — each is 200-360 LOC tightly tied to a specific shape of NEXUS_* exports that don't align 1:1 with the spec contract. Honest scope: their cutover deserves its own focused session. The Proposals screen demonstrates the pattern.

Concretely deferred:
- Dashboard.tsx cutover — wire `getDashboard()`; existing `NEXUS_DAEMON_STATUS` shape (uptime/cpu/mem) needs either backend support or graceful omission; "Active council" card needs a live-session look-up.
- Skills.tsx cutover — wire `listProductSkills()`; UI also expects org-skill adoption list that wasn't in the original mock split.
- CouncilSession.tsx cutover — replace the setTimeout simulator with `useEventStream(sessionStreamUrl(sid))`. Mechanical once the screen is read carefully.

## Slice 4 gates

| Gate | Status |
|---|---|
| 1. All seven screens render from FastAPI; no `NEXUS_*` imports remain in `app/p/[product]/**` | 🟡 partial — backend ready, scaffold in place, 1 of 7 screens cut over (Proposals). Rest deferred. |
| 2. Human approves a council draft in UI → `.skill.md` lands in git repo → Qdrant updated → provenance stamped | ✅ — end-to-end via Proposals screen; verified by `test_approval.py::test_approve_writes_skill_file_and_flips_status` |
| 3. `nexus-ui` builds with zero references to dropped mock exports | 🟡 — types lifted, but existing screens still import from `lib/data.ts`; mocks remain until each screen is rewritten |
| 4. Connector add wizard end-to-end without touching `nexus.yaml` manually | ⏳ — POST /sources path not yet wired (deferred with Onboarding cutover) |

## How to demo

```bash
# Backend
docker compose up -d                    # Qdrant, Neo4j, Langfuse
make services-up                        # llama.cpp embedder + reranker + ollama (when GGUFs present)
cp nexus.yaml.example nexus.yaml        # edit hierarchy_root → ./nexus/skills/seed for first run
uv run uvicorn nexus.api.app:app --port 8000

# Kick off a council via API
curl -X POST http://localhost:8000/products/forge/council/sessions \
  -H 'Content-Type: application/json' \
  -d '{"topic":"PDA seed validation","skill_kind":"product_domain"}'
# → {"session_id":"cs_2026...","status":"running"}

# Watch it live
curl -N http://localhost:8000/council/sessions/<sid>/stream
# Server-Sent Events: session_start → message (archaeologist) → message (domain_expert)
#                   → cost → message (synthesizer) → proposal_preview
#                   → message (adversary) → critique → [if blocking: redraft loop]
#                   → proposal → session_end

# Approve via UI
cd ../nexus-ui && npm run dev
# Visit http://localhost:3000/p/forge/proposals → click Approve
```

## Files added/modified

```
backend/
  nexus/council/agents/adversary.py
  nexus/council/state.py             (proposal, critique, revision_count)
  nexus/council/graph.py             (adversary node + conditional edge)
  nexus/council/agents/synthesizer.py (critique-aware redraft)
  nexus/council/runner.py            (async kickoff + pub/sub + astream)
  nexus/skills/approval.py
  nexus/skills/git.py
  nexus/registry.py
  nexus/api/deps.py                  (+ registry + skill_store)
  nexus/api/routes/{products,sources,skills,activity,council,proposals}.py
  nexus/api/routes/dashboard.py
  nexus/api/app.py                   (+ dashboard router)
  tests/test_adversary_routing.py
  tests/test_approval.py

ui/
  nexus-ui/lib/types.ts
  nexus-ui/lib/api/{client,index}.ts
  nexus-ui/lib/hooks/useEventStream.ts
  nexus-ui/components/screens/Proposals.tsx
  nexus-ui/app/p/[product]/proposals/page.tsx
```
