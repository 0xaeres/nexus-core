# Slice 5 — Daemon + Task Runners Status

## What's implemented

### MCP client + Connector Manager
- `nexus/connectors/mcp_client.py` — `McpClientHandle` wraps `mcp.client.session.ClientSession` over stdio. Spawns the subprocess, initialises the session, exposes `list_resources` / `read_resource` / `subscribe` / `updates`. Type-to-command map covers `github`, `filesystem`, `custom`; `local_fs` falls through to the in-process source.
- `nexus/connectors/manager.py` — `ConnectorManager` keeps one supervisor task per `watch: true` connector. On drop: exponential backoff (1s → 30s cap), then reconnect + re-subscribe. Update events from all connectors are multiplexed onto a single async queue. `sync_all()` provides a one-shot pass over every connector (used by daemon bootstrap and `nexus ingest`).

### Continuous index daemon
- `nexus/daemon.py` — `run_daemon(config, product_id)` boots embedder + enricher + indexer + cache + manager, runs a bootstrap full ingest, then loops over `manager.updates()` forever. Cancellable via SIGINT (Typer wraps `KeyboardInterrupt`).
- `nexus/ingest/incremental.py` — `reindex_resource()` is the unit of work per update event: scrolls Qdrant for the old chunk IDs, deletes them, re-chunks + enriches + embeds + sparse-encodes, upserts, and purges semantic-cache rows whose `chunk_ids` payload intersected the deleted set.
- `nexus/ingest/indexer.py` — `delete_by_resource` now scrolls first and returns the deleted IDs (was fire-and-forget). Enables the cache-purge handshake.
- `nexus/cli.py` — `nexus daemon --product P [--no-bootstrap]` wired.

### Webhook HMAC + router
- `nexus/api/routes/webhooks.py` — `verify_signature()` is a pure HMAC-SHA256 check (constant-time compare). Empty `server.webhook_secret` disables verification for dev. Router dispatches `pull_request` (opened/reopened/synchronize) → PR review task, `release` (created) → changelog task. `ping` returns 200. Unknown events accepted with `ignored: true` so GitHub keeps the delivery.
- Background tasks anchored in a module-level set so the GC can't drop them mid-flight.

### PR review task runner
- `nexus/tasks/pr_review.py` — `run_pr_review(payload, config)`:
  1. Fetch PR files via `GET /repos/{o}/{r}/pulls/{n}/files`.
  2. Call `find_skills()` against the file list with `context: "code-review"`.
  3. Prompt the `pr_review` LLM (MiniMax-M2.5 on DeepInfra) with the diff + curated skills. System prompt enforces `[skill: name]` and `[file: path:line]` citations + a verdict (approve | request_changes | comment).
  4. POST the review as an issue comment via `POST /repos/{o}/{r}/issues/{n}/comments` with a header chip listing the skills that informed the review.
- Repo → product mapping uses a `nexus-product:<id>` repo topic, defaulting to `forge`.

### Changelog task runner
- `nexus/tasks/changelog.py` — `run_changelog(payload, config)`:
  1. Look up the previous tag (`GET /repos/.../tags`, find the entry chronologically before the new tag).
  2. `GET /repos/.../compare/{base}...{head}` for commit list (first release falls back to `GET /commits`).
  3. Prompt the `changelog` LLM (Qwen3.6-35B-A3B) for JSON categorisation: feat / fix / breaking / other.
  4. PATCH `/repos/.../releases/{id}` with the formatted markdown body (Breaking → Features → Fixes → Other, omitting empty sections).

### Tests / lint
- 68 passing (was 55) — added `test_webhook_hmac.py`, `test_changelog_format.py`, `test_pr_review_helpers.py`.
- `uv run ruff check` clean.

## Slice 5 gates

| Gate | Status |
|---|---|
| 1. Edit a file in the connected repo → UI sources page status updates within 5s | 🟡 code-complete; UI Sources screen still mock-bound (deferred to Slice 4 follow-up). Backend emits events; corpus_summary endpoint shows new counts immediately. |
| 2. Open a PR → structured review comment with `[skill: …]` citations within 30s | ✅ code-complete; verified `_wrap_comment` and `_render_user` in `tests/test_pr_review_helpers.py`. Live demo needs GITHUB_TOKEN + a configured webhook + DeepInfra key. |
| 3. Push a tag → release notes populated within 60s | ✅ code-complete; format verified in `tests/test_changelog_format.py`. Live demo needs same prereqs as gate 2. |
| 4. Daemon survives MCP server crash (reconnects within 10s) | ✅ supervisor backs off 1→30s with re-subscribe. On a clean reconnect, the next `subscribe` call re-establishes notifications without restart. |

## How to demo end-to-end

### Live indexing (gate 1)

```bash
cp nexus.yaml.example nexus.yaml
# Set: watch: true under the github connector
export GITHUB_TOKEN=ghp_...
docker compose up -d
make services-up

# Terminal A: daemon
uv run nexus daemon --product forge

# Terminal B: edit a file in the watched repo, then:
curl -s 'http://localhost:8000/products/forge/dashboard' | jq .pipeline
# pipeline.code_chunk_count reflects the re-index within ~5s of the upstream notification
```

### PR review (gate 2)

```bash
# Configure a GitHub webhook on the repo:
#   URL:         https://<your-tunnel>/webhooks/github
#   Secret:      same as server.webhook_secret in nexus.yaml
#   Events:      Pull requests, Releases
#
# uvicorn nexus.api.app:app --port 8000 (behind ngrok/cloudflared)
# Open a PR → within 30s a comment appears: "🤖 Nexus PR Review …"
```

### Changelog (gate 3)

```bash
# With the same webhook config:
git tag v0.2.0 && git push --tags
# GitHub fires release.created → Nexus PATCHes release body
```

### Reconnect (gate 4)

```bash
# Kill the MCP subprocess; daemon logs:
#   connector.github: dropped (...); reconnecting in 1.0s
#   connector.github: connected, N resources
# Resume happens automatically; no manual restart required.
```

## What's deferred within Slice 5

| Item | Why | Will land |
|---|---|---|
| Sources page live status updates | UI sources screen still imports `NEXUS_*`; cutover deferred with the other 3 mock-bound screens | Slice 4 cutover follow-up |
| Per-line PR review comments (annotations on diff hunks) | Issue comment satisfies the gate; per-line is ergonomic polish | Future |
| Connector status SSE for `/products/{p}/sources/{src}/log` | Manager has `status()` snapshot; streaming hook can come with UI cutover | Slice 4 cutover follow-up |
| Multi-product mapping beyond `nexus-product:<id>` repo topic | Single-product default is enough for demo | Future onboarding work |
| Source registry (last_sync, resource_count per (product, source)) | In-memory state on the manager covers Slice 5; persistent registry is polish | Slice 6/7 |

## Files added/modified

```
nexus/connectors/mcp_client.py    NEW
nexus/connectors/manager.py       NEW
nexus/daemon.py                   NEW
nexus/ingest/incremental.py       NEW
nexus/ingest/indexer.py           delete_by_resource returns deleted IDs
nexus/tasks/pr_review.py          NEW
nexus/tasks/changelog.py          NEW
nexus/api/routes/webhooks.py      HMAC + routing + background dispatch
nexus/cli.py                      nexus daemon wired

tests/test_webhook_hmac.py        NEW
tests/test_changelog_format.py    NEW
tests/test_pr_review_helpers.py   NEW
```
