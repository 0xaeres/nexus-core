"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nexus.api.authz import auth_enabled, prod_enabled
from nexus.api.deps import get_auth_store, get_config_dep, get_registry
from nexus.api.routes import (
    agent,
    auth,
    council,
    dashboard,
    metrics,
    products,
    proposals,
    setup,
    skills,
    sources,
)
from nexus.auth.store import CSRF_COOKIE, SESSION_COOKIE
from nexus.graph.store import create_graph_store
from nexus.ingest.enrichment_worker import EnrichmentWorker
from nexus.logging_config import setup_logging

setup_logging()
log = logging.getLogger(__name__)

_enrichment_stop: asyncio.Event | None = None
_enrichment_task: asyncio.Task | None = None


async def start_enrichment_worker() -> None:
    global _enrichment_stop, _enrichment_task
    config = get_config_dep()
    if not config.ingestion.enrichment_worker.enabled:
        return
    registry = get_registry()
    worker = EnrichmentWorker.from_config(registry=registry, config=config)
    stop = asyncio.Event()
    _enrichment_stop = stop

    async def _run() -> None:
        try:
            await worker.run_forever(stop=stop)
        finally:
            await worker.aclose()

    _enrichment_task = asyncio.create_task(_run())

    def _log_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "background enrichment worker stopped unexpectedly",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    _enrichment_task.add_done_callback(_log_done)


async def stop_enrichment_worker() -> None:
    global _enrichment_stop, _enrichment_task
    if _enrichment_stop is not None:
        _enrichment_stop.set()
    if _enrichment_task is not None:
        await _enrichment_task
    _enrichment_stop = None
    _enrichment_task = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _validate_production_config()
    await start_enrichment_worker()
    try:
        yield
    finally:
        await stop_enrichment_worker()


app = FastAPI(
    title="Nexus API",
    description="Backend for the Nexus context engine. See ENGINEERING.md §8.",
    version="0.0.1",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "NEXUS_ALLOWED_ORIGINS",
            "http://localhost:3000",
        ).split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id

    if request.method == "OPTIONS":
        response = await call_next(request)
        _set_security_headers(response, request_id)
        return response

    auth_response = await _authenticate_request(request)
    if auth_response is not None:
        _set_security_headers(auth_response, request_id)
        return auth_response

    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    _set_security_headers(response, request_id)
    log.info(
        "request id=%s method=%s path=%s status=%s elapsed_ms=%.1f",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


async def _authenticate_request(request: Request) -> JSONResponse | None:
    if not auth_enabled() or _is_public_path(request.url.path):
        return None

    bearer = request.headers.get("authorization", "")
    prefix = "Bearer "
    admin_key = os.getenv("NEXUS_ADMIN_API_KEY") or ""
    if bearer.startswith(prefix) and admin_key:
        token = bearer.removeprefix(prefix)
        if secrets.compare_digest(token, admin_key):
            request.state.user = {
                "id": "admin-api-key",
                "email": "admin-api-key@nexus.local",
                "role": "admin",
                "status": "approved",
            }
            request.state.auth_via = "api_key"
            return None

    store = get_auth_store()
    session_token = request.cookies.get(SESSION_COOKIE, "")
    resolved = store.user_for_session(session_token)
    if resolved is None:
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    user, session = resolved
    request.state.user = user
    request.state.session = session
    request.state.auth_via = "session"

    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _is_csrf_exempt(
        request.url.path
    ):
        header_token = request.headers.get("x-nexus-csrf", "")
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        expected = str(session.get("csrf_token") or "")
        if not (
            header_token
            and cookie_token
            and secrets.compare_digest(header_token, cookie_token)
            and secrets.compare_digest(header_token, expected)
        ):
            return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
    return None


def _auth_enabled() -> bool:
    return auth_enabled()


def _is_public_path(path: str) -> bool:
    public = {
        "/health",
        "/auth/login",
        "/auth/request-access",
        "/setup/status",
    }
    return path in public


def _is_csrf_exempt(path: str) -> bool:
    return path == "/metrics/web-vitals"


def _set_security_headers(response, request_id: str) -> None:
    response.headers["x-request-id"] = request_id
    response.headers.setdefault("x-content-type-options", "nosniff")
    response.headers.setdefault("x-frame-options", "DENY")
    response.headers.setdefault("referrer-policy", "no-referrer")
    response.headers.setdefault(
        "permissions-policy",
        "camera=(), microphone=(), geolocation=()",
    )


def _validate_production_config() -> None:
    if not prod_enabled():
        return
    required_env = [
        "NEXUS_TOKEN_KEY",
        "NEXUS_SECRET_KEY",
        "NEXUS_ADMIN_API_KEY",
        "NEXUS_BOOTSTRAP_ADMIN_EMAIL",
        "NEXUS_BOOTSTRAP_ADMIN_PASSWORD",
        "NEXUS_ALLOWED_ORIGINS",
        "NEXUS_API_DOMAIN",
    ]
    missing = [name for name in required_env if not (os.getenv(name) or "").strip()]
    cfg = get_config_dep()
    if not cfg.skills_repo:
        missing.append("skills_repo/NEXUS_SKILLS_REPO")
    if missing:
        raise RuntimeError(
            "production config missing required values: " + ", ".join(sorted(missing))
        )


class HealthDependencies(BaseModel):
    falkordb: str


class HealthResponse(BaseModel):
    status: str
    dependencies: HealthDependencies


@app.get("/health", tags=["meta"], response_model=HealthResponse)
async def health() -> HealthResponse:
    try:
        cfg = get_config_dep()
        graph_store = create_graph_store(cfg)
        try:
            falkor_ok = await graph_store.health()
        finally:
            await graph_store.aclose()
    except Exception as e:
        log.warning("health config/dependency check failed: %s", e)
        falkor_ok = False
    return HealthResponse(
        status="ok" if falkor_ok else "degraded",
        dependencies=HealthDependencies(
            falkordb="ok" if falkor_ok else "unavailable",
        ),
    )


app.include_router(products.router)
app.include_router(agent.router)
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(metrics.router)
app.include_router(sources.router)
app.include_router(council.router)
app.include_router(skills.router)
app.include_router(proposals.router)
app.include_router(setup.router)
