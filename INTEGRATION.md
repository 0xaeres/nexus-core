# UI Integration Map

The frontend at `~/Desktop/projects/nexus-ui/` consumes mock data from `lib/data.ts`. During Phase 4 (Slice 4) each screen is converted in-place to call the FastAPI backend defined in `ENGINEERING.md §11`. This document is the cutover checklist.

**Rule**: do not touch `nexus-ui/` until the corresponding backend route is implemented and returning real data. No mock-vs-live env flag; the mock export is deleted as soon as the screen is cut over.

## Reconciliation rules

Before the first screen cuts over, lift types out of the mock and reconcile to spec:

| Mock value | Spec value | Action at cutover |
|---|---|---|
| `SkillKind = 'master' \| 'tech_stack' \| 'language' \| 'security'` | `SkillKind = 'master' \| 'product_domain'` + `OrgSkillKind = 'tech_stack' \| 'language' \| 'security'` | Split into two enums in `lib/types.ts`. Skills screen separates product-skill list from "Adopted Standards" section. |
| `AgentRole` includes `stack_specialist`, `code_semantics`, `security` | `AgentRole = archaeologist \| domain_expert \| synthesizer \| adversary \| security_sentinel \| curator` | Drop `stack_specialist`, `code_semantics`. Rename `security → security_sentinel`. Add `curator`. Update `COUNCIL_ROSTERS`, `COUNCIL_AGENT_LABELS`, `COUNCIL_AGENT_HUES`. |

## Screen → Mock → API mapping

| Screen file | Backend route(s) | Status |
|---|---|---|
| `screens/Dashboard.tsx` | `GET /products/{id}/dashboard` | ✅ cut over |
| `screens/Sources.tsx` | `GET /products/{id}/sources` | ✅ cut over |
| `screens/ConnectorDetail.tsx` | `GET /products/{id}/sources/{name}` (+ SSE `…/log` placeholder) | ✅ cut over (sync log SSE awaits backend endpoint) |
| `screens/ConnectorNew.tsx` | `POST /products/{id}/sources` | ✅ cut over (registry-backed sources) |
| `screens/Skills.tsx` | `GET /products/{id}/skills` | ✅ cut over |
| `screens/CouncilLanding.tsx` | `GET /products/{id}/council/sessions` + `POST .../sessions` | ✅ cut over |
| `screens/CouncilSession.tsx` | `GET /council/sessions/{sid}` + SSE `…/stream` | ✅ cut over with live SSE |
| `screens/Activity.tsx` | `GET /products/{id}/activity` | ✅ cut over |
| `screens/Proposals.tsx` | `GET /proposals` + `POST .../approve\|reject` | ✅ already live (Slice 4) |
| `screens/Settings.tsx` | `GET /products/{id}/settings`, `GET /settings/org` | ✅ cut over (4 tabs: general, members, models, roster) |
| `screens/Onboarding.tsx` | `POST /products`, `POST .../sources`, `POST .../council/sessions` | ✅ cut over (4-step wizard chains all three) |

## Streaming endpoints (replace `setTimeout` simulators)

| Hook (build once) | Used by | Backend stream |
|---|---|---|
| `lib/hooks/useEventStream.ts` | `CouncilSession` (deliberation messages, cost meter tick) | `GET /council/sessions/{sid}/stream` |
| same hook | `ConnectorDetail` / ingestion progress | `GET /products/{id}/sources/{src}/log` |

## New deps required at cutover

Frontend additions (none needed in Phase 0):

- `react-markdown`, `rehype-highlight`, `shiki` — required for skill body rendering when the (currently nonexistent) skill detail screen is built. Defer to whenever that screen lands.
- `react-diff-viewer` — required for proposal diff pane on the proposal validation screen. Defer.

## Verification per cutover

A screen is "cut over" when:

1. Its imports include zero `NEXUS_*` from `lib/data.ts`.
2. The corresponding mock export is **deleted** from `lib/data.ts`.
3. The screen renders correctly with `npm run dev` against a live backend.
4. The screen handles loading and error states (no more synchronous mock data).
