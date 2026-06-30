"""Multi-product eval runs + live/replay SSE.

Mirrors the council async-job pattern (``anvay/api/routes/council.py`` +
``anvay/council/runner.py``): POST kicks off a background ``asyncio`` task,
progress streams over an in-process hub, and results are read back from the
filesystem artifacts the harness writes under ``artifacts/evals/``.

The eval is **unified**: one run scores every requested product through the
shipping ``retrieve_evidence`` path (``auto`` mode only — the query-rewrite
ablation was removed), producing a single :class:`EvalRunArtifact`. Each product
is still guarded with ``assert_product_access`` and unknown products are
rejected.

Job status lives in-process (ephemeral); the durable record is the per-run
``summary.json`` on disk, so history survives restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from anvay.api.authz import assert_product_access
from anvay.api.deps import get_config_dep, get_registry
from anvay.config import AnvayConfig
from anvay.registry import Registry
from evals.corpus import PRODUCTS
from evals.harness import (
    DEFAULT_OUT_DIR,
    EvalRunArtifact,
    render_markdown,
    resolve_products,
    run_eval,
)
from evals.ingest import ensure_ingested

log = logging.getLogger(__name__)

router = APIRouter(tags=["evals"])

_CONFIG_PATH = Path("anvay.yaml")
_DEFAULT_MODES = ("auto",)


# ---------------------------------------------------------------- shapes


class EvalJobRef(BaseModel):
    job_id: str
    status: str


class EvalJobStatus(BaseModel):
    job_id: str
    status: str  # running | completed | failed
    products: list[str]
    modes: list[str]
    started_at: str
    completed_at: str | None = None
    run_id: str | None = None
    error: str | None = None


class ProductEvalInfo(BaseModel):
    product_id: str
    language: str
    needs_ingest: bool


class StartRunBody(BaseModel):
    products: list[str]
    limit: int | None = None
    top_k: int = 10
    ingest: bool = True


# ---------------------------------------------------------------- job hub


class _JobHub:
    """One asyncio.Queue per job_id; SSE readers fan out via queues."""

    _END = "__end__"

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict | str]]] = {}
        self._live: set[str] = set()
        self._completed: set[str] = set()
        self._lock = asyncio.Lock()

    async def start(self, job_id: str) -> None:
        async with self._lock:
            self._live.add(job_id)
            self._completed.discard(job_id)

    async def publish(self, job_id: str, event: dict) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(job_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("eval job %s: subscriber queue full; dropping event", job_id)

    async def finish(self, job_id: str) -> None:
        async with self._lock:
            self._live.discard(job_id)
            self._completed.add(job_id)
            queues = list(self._subscribers.get(job_id, []))
        for q in queues:
            await q.put(self._END)

    async def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        async with self._lock:
            self._subscribers.setdefault(job_id, []).append(q)
            if job_id in self._completed:
                await q.put(self._END)
        return q

    async def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        async with self._lock:
            queues = self._subscribers.get(job_id)
            if queues and q in queues:
                queues.remove(q)
            if not queues:
                self._subscribers.pop(job_id, None)

    def is_live(self, job_id: str) -> bool:
        return job_id in self._live and job_id not in self._completed


HUB = _JobHub()
# In-process job status records, keyed by job_id (ephemeral; disk is durable).
_JOBS: dict[str, EvalJobStatus] = {}
# Anchor background tasks so the GC does not cancel them mid-flight.
_RUNNING: set[asyncio.Task] = set()

_MAX_JOBS = 200
_EVICT_COUNT = 50


def _evict_stale_jobs() -> None:
    """Keep _JOBS bounded. Evict oldest terminal jobs when the dict grows too large.

    The durable record is the per-run ``summary.json`` on disk, so eviction is
    safe — callers can still load historical runs via the filesystem endpoints.
    """
    if len(_JOBS) <= _MAX_JOBS:
        return
    terminal = [
        (jid, status)
        for jid, status in _JOBS.items()
        if status.status in {"completed", "failed"}
    ]
    terminal.sort(key=lambda t: t[1].started_at)
    for jid, _ in terminal[:_EVICT_COUNT]:
        _JOBS.pop(jid, None)
        # Also clean up the hub's completed set so memory stays bounded.
        HUB._completed.discard(jid)


def _make_job_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"ej_{ts}_{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------- runner


async def _run_eval_job(
    *,
    job_id: str,
    products: list[str],
    modes: tuple[str, ...],
    limit: int | None,
    top_k: int,
    ingest: bool,
    config: AnvayConfig,
) -> None:
    status = _JOBS[job_id]
    try:
        product_evals = resolve_products(products)
        if ingest:
            for pe in product_evals:
                await HUB.publish(
                    job_id, {"event": "ingest_start", "data": {"product_id": pe.product_id}}
                )
                await ensure_ingested(pe, config=config)
        await HUB.publish(job_id, {"event": "eval_start", "data": {"products": products}})
        artifact = await run_eval(
            config=config,
            config_path=_CONFIG_PATH,
            products=product_evals,
            modes=modes,
            top_k=top_k,
            limit=limit,
            out_dir=DEFAULT_OUT_DIR,
        )
        status.run_id = artifact.run_id
        status.status = "completed"
        status.completed_at = datetime.now(UTC).isoformat()
        await HUB.publish(
            job_id,
            {"event": "job_done", "data": {"run_id": artifact.run_id, "passed": artifact.passed}},
        )
    except Exception as e:  # pragma: no cover - defensive
        log.exception("eval job %s crashed", job_id)
        status.status = "failed"
        status.completed_at = datetime.now(UTC).isoformat()
        status.error = f"{type(e).__name__}: {e}"
        await HUB.publish(
            job_id, {"event": "error", "data": {"message": str(e), "type": type(e).__name__}}
        )
    finally:
        await HUB.finish(job_id)
        _evict_stale_jobs()


def _validate_products(request: Request, registry: Registry, products: list[str]) -> list[str]:
    if not products or products == ["all"]:
        products = list(PRODUCTS.keys())
    unknown = [pid for pid in products if pid not in PRODUCTS]
    if unknown:
        raise HTTPException(status_code=404, detail=f"unknown product(s): {', '.join(unknown)}")
    for pid in products:
        assert_product_access(request, registry, pid, action="council")
    return products


# ---------------------------------------------------------------- endpoints


@router.post("/evals/runs")
async def start_run(
    request: Request,
    body: StartRunBody = Body(...),
    config: AnvayConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> EvalJobRef:
    """Kick off a unified eval run across the requested products."""
    products = _validate_products(request, registry, body.products)
    modes = _DEFAULT_MODES

    job_id = _make_job_id()
    _JOBS[job_id] = EvalJobStatus(
        job_id=job_id,
        status="running",
        products=products,
        modes=list(modes),
        started_at=datetime.now(UTC).isoformat(),
    )
    await HUB.start(job_id)
    task = asyncio.create_task(
        _run_eval_job(
            job_id=job_id,
            products=products,
            modes=modes,
            limit=body.limit,
            top_k=body.top_k,
            ingest=body.ingest,
            config=config,
        ),
        name=f"eval-{job_id}",
    )
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return EvalJobRef(job_id=job_id, status="running")


@router.get("/evals/jobs/{job_id}")
async def get_job(
    job_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> EvalJobStatus:
    status = _JOBS.get(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="job not found")
    for pid in status.products:
        assert_product_access(request, registry, pid, action="read")
    return status


@router.get("/evals/jobs/{job_id}/stream")
async def job_stream(
    job_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> EventSourceResponse:
    """Live stream while the job runs; replay terminal event if already done."""
    status = _JOBS.get(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="job not found")
    for pid in status.products:
        assert_product_access(request, registry, pid, action="read")
    if HUB.is_live(job_id):
        return EventSourceResponse(_stream_events(job_id))

    async def replay() -> AsyncIterator[dict]:
        terminal = "error" if status.status == "failed" else "job_done"
        yield {
            "event": terminal,
            "data": json.dumps({"run_id": status.run_id, "error": status.error}),
        }

    return EventSourceResponse(replay())


@router.get("/evals/runs")
async def list_runs(
    request: Request,
    registry: Registry = Depends(get_registry),
) -> dict:
    """Every persisted run artifact the caller can access, newest first."""
    artifacts = [a for a in _load_artifacts() if a is not None]
    artifacts.sort(key=lambda a: a.generated_at, reverse=True)
    # Filter to products the caller is allowed to read.
    accessible = [
        a for a in artifacts
        if all(
            _can_access_product(request, registry, pid)
            for p in a.products
            for pid in [p.product_id]
        )
    ]
    return {"runs": [a.model_dump(mode="json") for a in accessible]}


@router.get("/evals/runs/{run_id}")
async def get_run(
    run_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> dict:
    artifact = _load_artifact(run_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="run not found")
    for p in artifact.products:
        assert_product_access(request, registry, p.product_id, action="read")
    payload = artifact.model_dump(mode="json")
    payload["markdown"] = render_markdown(artifact)
    return payload


@router.get("/evals/corpus")
async def get_corpus(
    request: Request,
    registry: Registry = Depends(get_registry),
) -> dict:
    """The product registry, filtered to products the caller can read."""
    products = [
        ProductEvalInfo(
            product_id=p.product_id,
            language=p.language,
            needs_ingest=p.source_path is None,
        )
        for p in PRODUCTS.values()
        if _can_access_product(request, registry, p.product_id)
    ]
    return {"products": products}


# ---------------------------------------------------------------- helpers


async def _stream_events(job_id: str) -> AsyncIterator[dict]:
    q = await HUB.subscribe(job_id)
    try:
        while True:
            item = await q.get()
            if item == _JobHub._END:
                return
            assert isinstance(item, dict)
            yield {
                "event": item.get("event", "message"),
                "data": json.dumps(item.get("data", {})),
            }
    finally:
        await HUB.unsubscribe(job_id, q)


def _can_access_product(request: Request, registry: Registry, product_id: str) -> bool:
    """Return True when the caller is allowed to read the given product."""
    try:
        assert_product_access(request, registry, product_id, action="read")
        return True
    except HTTPException:
        return False


def _load_artifacts() -> list[EvalRunArtifact | None]:
    return [_read_artifact(path) for path in sorted(DEFAULT_OUT_DIR.glob("*/summary.json"))]


def _load_artifact(run_id: str) -> EvalRunArtifact | None:
    path = DEFAULT_OUT_DIR / run_id / "summary.json"
    return _read_artifact(path) if path.exists() else None


def _read_artifact(path: Path) -> EvalRunArtifact | None:
    try:
        return EvalRunArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("could not parse eval artifact %s", path)
        return None
