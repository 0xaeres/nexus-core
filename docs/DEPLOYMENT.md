# Nexus Deployment Runbook

Target shape:

- Backend: Oracle VM, Docker Compose, Caddy TLS, FastAPI API, private Qdrant.
- Frontend: Vercel, same-origin `/api/nexus/*` proxy to the backend.
- Auth: Auth0 Universal Login, backend RS256/JWKS access-token validation,
  and Nexus-owned product membership authorization.
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
GITHUB_TOKEN=...
NEXUS_ENV=production
NEXUS_AUTH_MODE=auth0
NEXUS_TOKEN_KEY=...
NEXUS_SECRET_KEY=...
NEXUS_ADMIN_API_KEY=...
NEXUS_BOOTSTRAP_ADMIN_EMAIL=you@example.com
NEXUS_ALLOWED_ORIGINS=https://<your-vercel-app>.vercel.app
NEXUS_API_DOMAIN=api.example.com
NEXUS_SKILLS_REPO=https://github.com/<org>/nexus-skills.git
NEXUS_ENABLE_LOCAL_FS_SOURCES=false
AUTH0_DOMAIN=<tenant>.<region>.auth0.com
AUTH0_AUDIENCE=https://api.example.com
AUTH0_ISSUER=https://<tenant>.<region>.auth0.com/
```

Generate `NEXUS_TOKEN_KEY`:

```bash
uv run python -c "from nexus.auth.token_cipher import TokenCipher; print(TokenCipher.generate_key())"
```

Generate `NEXUS_SECRET_KEY` and `NEXUS_ADMIN_API_KEY`:

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
APP_BASE_URL=https://<your-vercel-app>.vercel.app
AUTH0_SECRET=<long-random-secret>
AUTH0_DOMAIN=<tenant>.<region>.auth0.com
AUTH0_CLIENT_ID=...
AUTH0_CLIENT_SECRET=...
AUTH0_AUDIENCE=https://api.example.com
NEXUS_API_URL=https://api.example.com
```

Deploy the frontend repo. Browser calls stay same-origin at `/api/nexus/*`;
the Vercel route handler attaches the Auth0 access token server-side before
calling the backend. Confirm the Vercel domain is listed in backend
`NEXUS_ALLOWED_ORIGINS`.

## 5. First Login

Open the Vercel app and sign in through Auth0 using
`NEXUS_BOOTSTRAP_ADMIN_EMAIL`. The backend creates that email as the sole
initial Nexus admin on first valid Auth0 login. Other Auth0 users can sign in,
request access, and remain pending until an admin approves them.

## 6. Access Requests

Users can visit `/request-access`. Admin approves from:

```text
/admin/access
```

Approval assigns the user a Nexus app role; Auth0 remains the identity source.
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
- Requests without a valid Auth0 bearer token fail.
- Non-admin users see only their own products.
- Product viewers cannot sync sources, run council, or approve proposals.
- Filesystem sources are rejected in production.
- Add source -> sync source -> SSE logs stream.
- Run council -> Langfuse trace appears when configured.
- Approve proposal -> skill Git commit/push succeeds before proposal status
  becomes `approved`.
