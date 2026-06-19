# Nexus Deployment Runbook

Target shape:

- Backend: Oracle VM, Docker Compose, Caddy TLS, FastAPI API, private Qdrant.
- Frontend: Vercel, same-origin `/api/nexus/*` proxy to the backend.
- Auth: Nexus password/session auth with secure HttpOnly cookies, CSRF checks,
  bootstrap admin, and Nexus-owned product membership authorization.
- LLMOps: Langfuse Cloud free tier when `LANGFUSE_*` keys are configured.

## 1. Prepare Oracle VM

Install Docker and the Compose plugin, then open only ports `80` and `443` in
Oracle Cloud firewall/security list. Do not expose Qdrant ports publicly.

Clone the backend repo on the VM:

```bash
git clone <backend-repo-url> nexus
cd nexus
```

Create production config:

```bash
cp nexus.prod.yaml.example nexus.yaml
cp .env.example .env
```

## 2. Required Backend Environment

Fill `.env`:

```bash
DEEPINFRA_API_KEY=...
NEXUS_ENV=production
NEXUS_TOKEN_KEY=...
NEXUS_SECRET_KEY=...
NEXUS_ADMIN_API_KEY=...
NEXUS_BOOTSTRAP_ADMIN_EMAIL=you@example.com
NEXUS_BOOTSTRAP_ADMIN_PASSWORD=...
NEXUS_ALLOWED_ORIGINS=https://<your-vercel-app>.vercel.app
NEXUS_API_DOMAIN=api.example.com
NEXUS_SKILLS_REPO=https://github.com/<org>/nexus-skills.git
NEXUS_SKILLS_REPO_TOKEN=...
NEXUS_ENABLE_LOCAL_FS_SOURCES=false
```

Generate `NEXUS_TOKEN_KEY`:

```bash
uv run python -c "from nexus.auth.token_cipher import TokenCipher; print(TokenCipher.generate_key())"
```

Generate `NEXUS_SECRET_KEY`, `NEXUS_ADMIN_API_KEY`, and
`NEXUS_BOOTSTRAP_ADMIN_PASSWORD`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Optional Langfuse:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
NEXUS_TRACE_CONTENT=false
```

Keep `NEXUS_TRACE_CONTENT=false` unless you explicitly want prompt/response
content in Langfuse.

## 3. Start Backend

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

Health check:

```bash
curl -i https://api.example.com/health
```

Expected:

```json
{"status":"ok"}
```

Qdrant should not be reachable from public internet:

```bash
curl -i https://api.example.com:6333
```

That should fail.

## 4. Configure Vercel

In Vercel project settings, set:

```bash
NEXUS_API_URL=https://api.example.com
```

Deploy the frontend repo. Browser calls stay same-origin at `/api/nexus/*`;
the Vercel route handler forwards session cookies and CSRF headers to the
backend. Confirm the Vercel domain is listed in backend `NEXUS_ALLOWED_ORIGINS`.

## 5. First Login

Open the Vercel app and sign in with `NEXUS_BOOTSTRAP_ADMIN_EMAIL` plus
`NEXUS_BOOTSTRAP_ADMIN_PASSWORD`. The backend creates that account as the sole
initial Nexus admin on first boot. Other users can request access and remain
pending until an admin approves them.

## 6. Access Requests

Users can visit `/request-access`. Admin approves from:

```text
/admin/access
```

Approval assigns the user a Nexus app role.
Revoking a user blocks future backend access.

## 7. Operations

View logs:

```bash
docker compose -f docker-compose.prod.yml logs -f api
```

Restart:

```bash
docker compose -f docker-compose.prod.yml restart api
```

Upgrade:

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

Backup mounted Docker volumes regularly:

- `nexus_data`: SQLite registry/proposals/sessions/checkpoints.
- `nexus_skills`: local skills repo clone.
- `qdrant_data`: vector index.

## 8. Smoke Test Checklist

- `/health` returns `200`.
- Vercel app loads login page.
- Admin login succeeds.
- `/products` works only after login.
- Requests without a valid session or admin API bearer token fail.
- Non-admin users see only their own products.
- Product viewers cannot sync sources, run council, or approve proposals.
- Filesystem sources are rejected in production.
- Add source -> sync source -> SSE logs stream.
- Run council -> Langfuse trace appears when configured.
- Approve proposal -> skill Git commit/push succeeds before proposal status
  becomes `approved`.
