"""Source connectors — see ENGINEERING.md §8.

Sources come from two places:
1. `nexus.yaml` `connectors:` block (declarative, baked in at deploy time).
2. The runtime registry (added via the UI; persists across restarts).

The list endpoint merges both; the registry wins on name conflicts so user-added
config can override the declarative defaults.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from git import Repo

from nexus.api.authz import assert_product_access, local_fs_enabled, rate_limit
from nexus.api.deps import get_config_dep, get_registry
from nexus.auth.token_cipher import TokenCipherError
from nexus.config import NexusConfig
from nexus.connectors.confluence import ConfluenceSource, confluence_config_from_source
from nexus.connectors.jira import JiraSource, jira_config_from_source
from nexus.connectors.local_fs import LocalFsConfig, LocalFsSource
from nexus.ingest.models import ResourceRef
from nexus.ingest.pipeline import IngestStats, run_ingest
from nexus.registry import Registry
from nexus.setup.bootstrap import _authenticated_clone_url

log = logging.getLogger(__name__)

# Per-source log queues: product_id:source_id → asyncio.Queue[dict | None]
# None signals end-of-stream to a waiting SSE subscriber.
_log_queues: dict[str, asyncio.Queue] = {}
_sync_tasks: dict[str, asyncio.Task] = {}

router = APIRouter(prefix="/products/{product_id}/sources", tags=["sources"])


_SECRET_KEY_HINTS = ("token", "api_key", "password", "secret")


def _redact(d: dict) -> dict:
    out: dict = {}
    for k, v in d.items():
        if any(s in k.lower() for s in _SECRET_KEY_HINTS):
            out[k] = "***" if v else ""
        else:
            out[k] = v
    return out


def _config_sources(config: NexusConfig, product_id: str) -> list[dict]:
    out: list[dict] = []
    for c in config.connectors:
        extras = c.model_dump(exclude={"name", "type", "watch"})
        out.append({
            "id": c.name,
            "product": product_id,
            "name": c.name,
            "type": c.type,
            "status": "watching" if c.watch else "connected",
            "lastSync": None,
            "resourceCount": 0,
            "config": _redact(extras),
        })
    return out


@router.get("")
async def list_sources(
    product_id: str,
    request: Request,
    config: NexusConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> dict:
    assert_product_access(request, registry, product_id)
    by_name = {s["name"]: s for s in _config_sources(config, product_id)}
    for s in registry.list_sources(product_id):
        s["config"] = _redact(s.get("config") or {})
        by_name[s["name"]] = s
    enrichment = registry.enrichment_job_counts(product_id)
    for source in by_name.values():
        source["enrichment"] = enrichment
    return {"sources": list(by_name.values())}


@router.get("/{source_id}")
async def get_source(
    source_id: str,
    product_id: str,
    request: Request,
    config: NexusConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> dict:
    assert_product_access(request, registry, product_id)
    runtime = registry.get_source(product_id, source_id)
    if runtime:
        runtime["config"] = _redact(runtime.get("config") or {})
        runtime["enrichment"] = registry.enrichment_job_counts(product_id)
        return runtime
    for s in _config_sources(config, product_id):
        if s["name"] == source_id:
            s["enrichment"] = registry.enrichment_job_counts(product_id)
            return s
    raise HTTPException(status_code=404, detail="source not found")


@router.post("")
async def add_source(
    product_id: str,
    request: Request,
    name: str = Body(..., embed=True),
    type: str = Body(..., embed=True),
    config_block: dict = Body(default_factory=dict, embed=True, alias="config"),
    registry: Registry = Depends(get_registry),
) -> dict:
    assert_product_access(request, registry, product_id, action="source")
    if type in {"filesystem", "local_fs"} and not local_fs_enabled():
        raise HTTPException(status_code=403, detail="filesystem sources are disabled")
    if registry.get_source(product_id, name):
        raise HTTPException(status_code=409, detail=f"source {name!r} already exists")
    try:
        registry.upsert_source(
            {
                "product": product_id,
                "name": name,
                "type": type,
                "status": "connected",
                "config": config_block,
                "resourceCount": 0,
            }
        )
    except TokenCipherError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    out = registry.get_source(product_id, name) or {}
    out["config"] = _redact(out.get("config") or {})
    return out


@router.delete("/{source_id}")
async def delete_source(
    product_id: str,
    source_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> dict:
    assert_product_access(request, registry, product_id, action="source")
    if not registry.delete_source(product_id, source_id):
        raise HTTPException(status_code=404, detail="source not found in registry")
    return {"ok": True}


@router.post("/{source_id}/sync")
async def sync_source(
    product_id: str,
    source_id: str,
    request: Request = None,  # type: ignore[assignment]
    config: NexusConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> dict:
    if request is not None:
        assert_product_access(request, registry, product_id, action="source")
        rate_limit(request, bucket="source_sync", limit=60, window_s=86400)
    runtime = registry.get_source(product_id, source_id)
    config_sources = {s["name"]: s for s in _config_sources(config, product_id)}
    if not runtime and source_id not in config_sources:
        raise HTTPException(status_code=404, detail="source not found")

    source = runtime or config_sources[source_id]
    if source.get("type") in {"filesystem", "local_fs"} and not local_fs_enabled():
        raise HTTPException(status_code=403, detail="filesystem sources are disabled")
    key = f"{product_id}:{source_id}"
    existing = _sync_tasks.get(key)
    if existing and not existing.done():
        return {
            "ok": True,
            "queued": False,
            "already_running": True,
            "product": product_id,
            "source": source_id,
        }
    if existing and existing.done():
        _sync_tasks.pop(key, None)

    q: asyncio.Queue = asyncio.Queue(maxsize=2048)
    _log_queues[key] = q
    if runtime:
        registry.upsert_source({**runtime, "status": "syncing"})

    async def _run() -> None:
        await _sync_source_contents(
            product_id=product_id,
            source=source,
            runtime=runtime,
            config=config,
            registry=registry,
            q=q,
        )

    task = asyncio.create_task(_run())
    _sync_tasks[key] = task

    def _cleanup_task(t: asyncio.Task) -> None:
        _sync_tasks.pop(key, None)
        if not t.cancelled():
            t.exception()

    task.add_done_callback(_cleanup_task)
    return {"ok": True, "queued": True, "product": product_id, "source": source_id}


# ---------------------------------------------------------------- sync execution


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _emit(q: asyncio.Queue, level: str, msg: str, **extra) -> None:
    log.info("source_sync.%s %s extra=%s", level, msg, extra)
    try:
        await q.put({"level": level, "msg": msg, "ts": _now(), **extra})
    except Exception:
        # Queue full or closed — degrade silently. The pipeline keeps going.
        log.debug("ingest log queue rejected: %s %s", level, msg)


class _CanonicalProgressSource:
    """Wraps a LocalFsSource to emit structured progress events per resource read.

    Implements the `_Source` protocol from nexus.ingest.pipeline.
    """

    def __init__(
        self,
        inner: LocalFsSource,
        *,
        root: Path,
        source_id: str,
        canonical_prefix: str | None,
        put: Callable[..., None],
        total: int,
    ):
        self.inner = inner
        self.root = root.resolve()
        self.source_id = source_id
        self.canonical_prefix = canonical_prefix
        self.put = put
        self.total = total
        self.processed = 0
        self._real_by_uri: dict[str, ResourceRef] = {}

    async def list_resources(self) -> AsyncIterator[ResourceRef]:
        async for ref in self.inner.list_resources():
            canonical_uri = self._canonical_uri(ref)
            canonical = ResourceRef(
                source_id=self.source_id,
                uri=canonical_uri,
                mime=ref.mime,
                size_bytes=ref.size_bytes,
                last_modified=ref.last_modified,
            )
            self._real_by_uri[canonical_uri] = ref
            yield canonical

    async def read_resource(self, ref: ResourceRef) -> str:
        self.processed += 1
        pct = round(100 * self.processed / self.total) if self.total else 0
        short = ref.uri.rsplit("/", 1)[-1] or ref.uri
        self.put(
            "progress",
            f"Reading {self.processed} of {self.total} — {short[:80]}",
            done=self.processed,
            total=self.total,
            pct=pct,
            stage="read",
            uri=ref.uri,
        )
        real = self._real_by_uri.get(ref.uri, ref)
        return await self.inner.read_resource(real)

    def _canonical_uri(self, ref: ResourceRef) -> str:
        if self.canonical_prefix is None:
            return ref.uri
        rel = Path(ref.uri).resolve().relative_to(self.root)
        return f"{self.canonical_prefix}/{rel.as_posix()}"


async def _sync_source_contents(
    *,
    product_id: str,
    source: dict,
    runtime: dict | None,
    config: NexusConfig,
    registry: Registry,
    q: asyncio.Queue,
) -> None:
    """Resolve source roots, run ingest, update source state, and stream progress."""
    src_type = source.get("type", "unknown")
    cleanup_dirs: list[Path] = []
    try:
        await _emit(q, "info", f"Starting sync for '{source.get('name')}' ({src_type})")

        if src_type == "github":
            roots = await _clone_github_repos(source, q)
            cleanup_dirs.extend(cleanup for _, _, cleanup in roots)
        elif src_type == "jira":
            stats = await _ingest_jira_source(
                product_id=product_id,
                source=source,
                registry=registry,
                config=config,
                q=q,
            )
            await _finish_source_sync(
                stats=stats,
                source=source,
                runtime=runtime,
                registry=registry,
                q=q,
            )
            return
        elif src_type == "confluence":
            stats = await _ingest_confluence_source(
                product_id=product_id,
                source=source,
                registry=registry,
                config=config,
                q=q,
            )
            await _finish_source_sync(
                stats=stats,
                source=source,
                runtime=runtime,
                registry=registry,
                q=q,
            )
            return
        elif src_type in ("filesystem", "local_fs"):
            if not local_fs_enabled():
                await _emit(q, "error", "filesystem sources are disabled")
                return
            roots = source.get("config", {}).get("roots") or [
                source.get("config", {}).get("root")
            ]
            if not roots or not roots[0]:
                await _emit(q, "error", "filesystem source missing 'root' in config")
                return
            local_root = Path(str(roots[0]))
            if not local_root.is_dir():
                await _emit(q, "error", f"filesystem root not a directory: {local_root}")
                return
            roots = [(source.get("name") or "filesystem", local_root, None)]
        else:
            await _emit(
                q,
                "error",
                f"Connector type {src_type!r} is not yet wired for sync. "
                "Currently supported: github, filesystem, jira, confluence.",
            )
            return

        total_stats = IngestStats()
        repo_map_symbols = []
        repo_map_file_count = 0

        source_name = source.get("name") or "source"
        for root_label, local_root, _cleanup in roots:
            source_key = f"{source_name}:{_repo_label_for_path(root_label)}"
            stats, rm = await _ingest_root(
                product_id=product_id,
                source_name=source_name,
                source_key=source_key,
                root_label=root_label,
                local_root=local_root,
                src_type=src_type,
                registry=registry,
                config=config,
                q=q,
            )
            total_stats.resources_seen += stats.resources_seen
            total_stats.resources_indexed += stats.resources_indexed
            total_stats.resources_skipped += stats.resources_skipped
            total_stats.resources_failed += stats.resources_failed
            total_stats.chunks_produced += stats.chunks_produced
            total_stats.chunks_indexed += stats.chunks_indexed
            total_stats.graph_resources_indexed += stats.graph_resources_indexed
            total_stats.graph_errors += stats.graph_errors
            total_stats.embed_errors += stats.embed_errors
            total_stats.added += stats.added
            total_stats.updated += stats.updated
            total_stats.removed += stats.removed
            total_stats.unchanged += stats.unchanged

            if rm:
                prefix = _repo_label_for_path(root_label)
                repo_map_file_count += rm.file_count
                repo_map_symbols.extend(
                    replace(sym, file=f"{prefix}/{sym.file}") for sym in rm.symbols
                )

        stats = total_stats

        if repo_map_symbols:
            try:
                from nexus.retrieval.repomap import RepoMap, repomap_path_for, save_repo_map

                combined = RepoMap(symbols=repo_map_symbols)
                state_dir = config.storage.proposal_queue.parent
                save_repo_map(combined, repomap_path_for(state_dir, product_id))
                await _emit(
                    q,
                    "info",
                    f"Repo map: {len(combined.symbols)} symbols across "
                    f"{repo_map_file_count} files",
                )
            except Exception as e:
                log.warning("repomap save failed: %s", e)
                await _emit(q, "warn", f"Repo map save failed: {e} (council will still run)")

        await _finish_source_sync(
            stats=stats,
            source=source,
            runtime=runtime,
            registry=registry,
            q=q,
        )

    except Exception as e:
        log.exception(
            "sync_source failed for product=%s source=%s", product_id, source.get("name")
        )
        await _emit(q, "error", f"Ingest failed: {type(e).__name__}: {_redact_text(str(e))}")
        if runtime:
            try:
                registry.upsert_source({**runtime, "status": "error"})
            except Exception:
                log.debug("failed to mark source 'error' status")
    finally:
        for cleanup_dir in cleanup_dirs:
            await asyncio.to_thread(shutil.rmtree, str(cleanup_dir), ignore_errors=True)
        await q.put(None)


async def _ingest_root(
    *,
    product_id: str,
    source_name: str,
    source_key: str,
    root_label: str,
    local_root: Path,
    src_type: str,
    registry: Registry,
    config: NexusConfig,
    q: asyncio.Queue,
):
    await _emit(q, "info", f"Walking {root_label} at {local_root}…")

    def _put(level: str, msg: str, **extra) -> None:
        log.info("source_sync.%s repo=%s %s extra=%s", level, root_label, msg, extra)
        try:
            q.put_nowait({"level": level, "msg": msg, "ts": _now(), "repo": root_label, **extra})
        except asyncio.QueueFull:
            log.debug("ingest log queue full; dropping: %s", msg)

    fs_source = LocalFsSource(LocalFsConfig(root=local_root))

    await _emit(q, "info", f"Counting files for {root_label}…")
    total = 0
    async for _ in fs_source.list_resources():
        total += 1
    await _emit(
        q,
        "started",
        f"Starting ingestion for {root_label} — {total} files found",
        total=total,
        repo=root_label,
    )

    repo_label = _repo_label_for_path(root_label)
    canonical_prefix = f"github:{repo_label}" if src_type == "github" else None
    canonical_source_id = f"github:{repo_label}" if src_type == "github" else fs_source.source_id
    adapter = _CanonicalProgressSource(
        fs_source,
        root=local_root,
        source_id=canonical_source_id,
        canonical_prefix=canonical_prefix,
        put=_put,
        total=total,
    )

    async def _pipeline_event(event: dict) -> None:
        payload = dict(event)
        level = str(payload.pop("level", "stage"))
        msg = str(payload.pop("msg", ""))
        await _emit(q, level, msg, repo=root_label, **payload)

    await _emit(
        q,
        "info",
        f"Running ingest pipeline for {root_label} (chunk → embed → index)…",
    )
    run_id = registry.start_sync_run(product_id, source_key, _now())
    try:
        stats = await run_ingest(
            product_id=product_id,
            source=adapter,
            config=config,
            enrich=False,
            enrichment_mode="disabled",
            event_sink=_pipeline_event,
            registry=registry,
            source_key=source_key,
        )
    except Exception:
        registry.finish_sync_run(
            run_id,
            finished_at=_now(),
            added=0,
            updated=0,
            removed=0,
            unchanged=0,
            status="error",
        )
        raise
    registry.finish_sync_run(
        run_id,
        finished_at=_now(),
        added=stats.added,
        updated=stats.updated,
        removed=stats.removed,
        unchanged=stats.unchanged,
        status="done" if stats.resources_failed == 0 and stats.embed_errors == 0 else "partial",
    )
    rm = None
    try:
        from nexus.retrieval.repomap import extract_repo_map

        await _emit(q, "info", f"Building repo map for {root_label}…")
        rm = await asyncio.to_thread(extract_repo_map, local_root)
    except Exception as e:
        log.warning("repomap build failed for %s: %s", root_label, e)
        await _emit(q, "warn", f"Repo map build failed for {root_label}: {e}")

    return stats, rm


async def _ingest_jira_source(
    *,
    product_id: str,
    source: dict,
    registry: Registry,
    config: NexusConfig,
    q: asyncio.Queue,
) -> IngestStats:
    cfg = jira_config_from_source(source)
    jira_source = JiraSource(cfg)
    source_name = source.get("name") or "jira"
    source_key = f"{source_name}:{jira_source.source_id.removeprefix('jira:')}"

    async def _pipeline_event(event: dict) -> None:
        payload = dict(event)
        level = str(payload.pop("level", "stage"))
        msg = str(payload.pop("msg", ""))
        await _emit(q, level, msg, source=source_name, **payload)

    await _emit(q, "info", "Searching Jira issues with configured JQL")
    run_id = registry.start_sync_run(product_id, source_key, _now())
    try:
        stats = await run_ingest(
            product_id=product_id,
            source=jira_source,
            config=config,
            enrich=False,
            enrichment_mode="disabled",
            event_sink=_pipeline_event,
            registry=registry,
            source_key=source_key,
        )
    except Exception:
        registry.finish_sync_run(
            run_id,
            finished_at=_now(),
            added=0,
            updated=0,
            removed=0,
            unchanged=0,
            status="error",
        )
        raise
    finally:
        await jira_source.aclose()
    registry.finish_sync_run(
        run_id,
        finished_at=_now(),
        added=stats.added,
        updated=stats.updated,
        removed=stats.removed,
        unchanged=stats.unchanged,
        status="done"
        if stats.resources_failed == 0 and stats.embed_errors == 0
        else "partial",
    )
    return stats


async def _ingest_confluence_source(
    *,
    product_id: str,
    source: dict,
    registry: Registry,
    config: NexusConfig,
    q: asyncio.Queue,
) -> IngestStats:
    cfg = confluence_config_from_source(source)
    confluence_source = ConfluenceSource(cfg)
    source_name = source.get("name") or "confluence"
    source_key = f"{source_name}:{confluence_source.source_id.removeprefix('confluence:')}"

    async def _pipeline_event(event: dict) -> None:
        payload = dict(event)
        level = str(payload.pop("level", "stage"))
        msg = str(payload.pop("msg", ""))
        await _emit(q, level, msg, source=source_name, **payload)

    space_info = (
        f"spaces: {', '.join(cfg.space_keys)}" if cfg.space_keys else "all spaces"
    )
    await _emit(q, "info", f"Fetching Confluence pages ({space_info})")
    run_id = registry.start_sync_run(product_id, source_key, _now())
    try:
        stats = await run_ingest(
            product_id=product_id,
            source=confluence_source,
            config=config,
            enrich=False,
            enrichment_mode="disabled",
            event_sink=_pipeline_event,
            registry=registry,
            source_key=source_key,
        )
    except Exception:
        registry.finish_sync_run(
            run_id,
            finished_at=_now(),
            added=0,
            updated=0,
            removed=0,
            unchanged=0,
            status="error",
        )
        raise
    finally:
        await confluence_source.aclose()
    registry.finish_sync_run(
        run_id,
        finished_at=_now(),
        added=stats.added,
        updated=stats.updated,
        removed=stats.removed,
        unchanged=stats.unchanged,
        status="done"
        if stats.resources_failed == 0 and stats.embed_errors == 0
        else "partial",
    )
    return stats



async def _finish_source_sync(
    *,
    stats: IngestStats,
    source: dict,
    runtime: dict | None,
    registry: Registry,
    q: asyncio.Queue,
) -> None:
    if stats.embed_errors:
        await _emit(
            q,
            "warn",
            f"{stats.embed_errors} batch(es) failed to embed — "
            "chunks too large for embedder token limit. "
            "Raise EMBEDDER_UBATCH for the embedder and re-sync "
            "(1024 is the M2/8GB default; try 2048 on larger machines).",
        )
    if stats.graph_errors:
        await _emit(
            q,
            "warn",
            f"{stats.graph_errors} resource(s) failed graph extraction/write; "
            "vectors remain available and graph will retry next sync.",
        )
    await _emit(
        q,
        "success" if not stats.embed_errors else "done",
        (
            f"Sync complete — added={stats.added}, updated={stats.updated}, "
            f"removed={stats.removed}, unchanged={stats.unchanged}, "
            f"failed={stats.resources_failed}, "
            f"{stats.chunks_indexed} chunks in vector store"
        ),
    )

    if runtime:
        registry.upsert_source({
            **runtime,
            "lastSync": _now(),
            "resourceCount": stats.resources_seen,
            "status": "connected",
        })


def _github_repo_urls(source: dict) -> list[str]:
    cfg = source.get("config") or {}
    repos = cfg.get("repos") or []
    if not repos:
        raise ValueError(
            "github source has no 'repos' configured (expected a GitHub URL)"
        )
    urls = [str(repo).strip().rstrip("/").removesuffix(".git") for repo in repos]
    invalid = [
        url
        for url in urls
        if not url.startswith("https://github.com/") and not url.startswith("git@github.com:")
    ]
    if invalid:
        bad = ", ".join(repr(url) for url in invalid)
        raise ValueError(
            f"unsupported github URL(s): {bad} "
            "(expected https://github.com/<org>/<repo> or git@github.com:<org>/<repo>)"
        )
    return urls


def _redact_text(text: str) -> str:
    return re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", text)


async def _clone_github_repos(
    source: dict, q: asyncio.Queue
) -> list[tuple[str, Path, Path]]:
    cfg = source.get("config") or {}
    token = cfg.get("token") or None
    urls = _github_repo_urls(source)
    roots = []
    for index, url in enumerate(urls, start=1):
        roots.append((url, *await _clone_github_repo(url, token, q, index, len(urls))))
    return roots


async def _clone_github_repo(
    url: str,
    token: str | None,
    q: asyncio.Queue,
    index: int,
    total: int,
) -> tuple[Path, Path]:
    """Shallow-clone one GitHub repo into a temp dir. Returns (root, cleanup_dir)."""
    await _emit(q, "info", f"Cloning repo {index}/{total}: {url} (shallow, depth=1)…")
    tmp = Path(tempfile.mkdtemp(prefix="nexus-ingest-"))
    clone_path = tmp / "repo"
    auth_url = _authenticated_clone_url(url + ".git", token)

    try:
        await asyncio.to_thread(
            Repo.clone_from, auth_url, str(clone_path), depth=1, multi_options=["--quiet"]
        )
    except Exception as e:
        shutil.rmtree(str(tmp), ignore_errors=True)
        raise RuntimeError(f"git clone failed: {_redact_text(str(e))}") from e

    await _emit(q, "info", "Clone complete")
    return clone_path, tmp


def _repo_label_for_path(label: str) -> str:
    label = label.rstrip("/").removesuffix(".git")
    if label.startswith("https://github.com/"):
        return label.removeprefix("https://github.com/")
    if label.startswith("git@github.com:"):
        return label.removeprefix("git@github.com:")
    return label.strip("/") or "source"


@router.get("/{source_id}/log")
async def source_log_stream(
    product_id: str,
    source_id: str,
    request: Request,
    config: NexusConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> StreamingResponse:
    assert_product_access(request, registry, product_id)
    runtime = registry.get_source(product_id, source_id)
    config_sources = {s["name"]: s for s in _config_sources(config, product_id)}
    if not runtime and source_id not in config_sources:
        raise HTTPException(status_code=404, detail="source not found")

    key = f"{product_id}:{source_id}"

    async def event_stream():
        q = _log_queues.get(key)
        if q is None:
            yield f"data: {json.dumps({'level': 'done', 'msg': 'No active sync. Trigger one with the Sync button.'})}\n\n"
            return
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=30.0)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if item is None:
                    yield f"data: {json.dumps({'level': 'done', 'msg': 'Sync complete'})}\n\n"
                    _log_queues.pop(key, None)
                    return
                yield f"data: {json.dumps(item)}\n\n"
        except asyncio.CancelledError:
            _log_queues.pop(key, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
