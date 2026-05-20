# UI Cutover - Complete

Every screen in `nexus-ui/components/screens/` consumes the live FastAPI
backend. No `NEXUS_*` mock exports remain in the UI tree.

## Final inventory

| Screen | Backend | Notes |
|---|---|---|
| `Dashboard` | `GET /products/{id}/dashboard` | pipeline + pending + recent activity |
| `Skills` | `GET /products/{id}/skills` | master / domain / adopted; detail pane |
| `CouncilLanding` | `GET .../sessions`, `POST .../sessions` | start-session dialog + redirect |
| `CouncilSession` | `GET /council/sessions/{sid}` + SSE `/stream` | live deliberation via `useEventStream`; approve/reject inline |
| `Sources` | `GET /products/{id}/sources` | registry-merged with config-defined connectors |
| `ConnectorDetail` | `GET .../sources/{id}` | sync-log SSE placeholder pending backend endpoint |
| `ConnectorNew` | `POST /products/{id}/sources` | 6 connector templates + custom MCP escape hatch |
| `Activity` | `GET /products/{id}/activity` | type filter tabs |
| `Settings` | `GET .../settings` | 4 tabs: general / members / models / roster |
| `Onboarding` | `POST /products` → `POST .../sources` → `POST .../council/sessions` | 4-step wizard, chains all three |
| `Proposals` | `GET /proposals` + approve/reject | reference screen from Slice 4 |

## Backend additions for the cutover

- `POST /products` — Registry.upsert_product
- `POST /products/{id}/sources` — Registry-backed; merged with `nexus.yaml` connectors at read time
- `DELETE /products/{id}/sources/{name}` — removes registry entries only (config-defined are immutable)
- `POST /products/{id}/sources/{name}/sync` — acknowledgement endpoint (real sync = daemon work)
- `GET /products/{id}/settings` — members from Registry + model assignments from config (api_key redacted)
- `GET /settings/org` — admins + members + billing placeholder
- `Registry.list_sources / get_source / upsert_source / delete_source` mixin

## Dead code removed

```
nexus-ui/lib/data.ts                              DELETED  (everything dead)
nexus-ui/lib/agent-colors.ts                      DELETED  (replaced by COUNCIL_AGENT_HUES from lib/types.ts)
nexus-ui/lib/selectors.ts                         DELETED  (no callers)
nexus-ui/components/pipeline/Pipeline.tsx         DELETED  (Dashboard no longer needs it)
nexus-ui/components/sources/IngestionProgress.tsx DELETED  (no callers)
nexus-ui/components/pipeline/                     DELETED  (empty)
nexus-ui/components/sources/                      DELETED  (empty)
```

## Verification

- `npx tsc --noEmit` from `nexus-ui/` — clean
- `uv run ruff check nexus tests evals` — clean
- `uv run pytest` — 104 passing

## End-to-end demo

Cold-start from scratch:

```bash
# Backend
docker compose up -d
make services-up
cp nexus.yaml.example nexus.yaml
uv run uvicorn nexus.api.app:app --port 8000

# UI
cd ../nexus-ui && npm run dev
# Open http://localhost:3000

# Walk it
# 1. App boots, /me + /products resolve, redirected to /p/forge/dashboard
# 2. Click "Add source" → ConnectorNew → pick GitHub → POST /sources → success
# 3. Sources page shows the new entry merged with nexus.yaml defaults
# 4. Council → "Start council session" → topic + kind → POST /sessions → redirect to live stream
# 5. CouncilSession: live SSE messages, costs, critique, proposal preview
# 6. When proposal arrives → Approve button → POST /approve → skill written to git
# 7. Skills page now shows the new skill in the tree
# 8. Settings → all 4 tabs render real data
# 9. Onboarding: /onboarding → 4-step wizard chains POST /products → POST /sources → POST /council/sessions → redirect to live session
```

Slices 0-7 + UI cutover = full system end-to-end demoable from the browser.
