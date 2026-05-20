"""Connector Manager - spawns MCP subprocesses, multiplexes update notifications.

Lifecycle:
  start()                     - launch every `watch: true` MCP connector
  sync_all(product_id)        - one-shot list_resources + read_resource pass
                                across every connector (MCP or local_fs)
  updates()                   - async iterator yielding (source_id, ResourceRef)
                                whenever a watched resource changes
  stop()                      - tear everything down

Reconnect: per-connector supervisor task with exponential backoff capped at 30s.
A drop emits a `connector_state` log event so the daemon can surface degraded
status to the UI.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from nexus.config import ConnectorCfg, NexusConfig
from nexus.connectors.local_fs import LocalFsConfig, LocalFsSource
from nexus.connectors.mcp_client import McpClientHandle
from nexus.ingest.models import ResourceRef

log = logging.getLogger(__name__)


@dataclass
class _ConnectorState:
    cfg: ConnectorCfg
    is_mcp: bool
    # populated only after a successful start
    handle: McpClientHandle | None = None
    local_fs: LocalFsSource | None = None
    task: asyncio.Task | None = None
    backoff_s: float = 1.0
    last_error: str | None = None
    resources_seen: int = 0


@dataclass
class ConnectorEvent:
    source_id: str
    resource: ResourceRef


class ConnectorManager:
    """One per process. Hands events to the daemon via `updates()`."""

    def __init__(self, config: NexusConfig):
        self.config = config
        self._states: dict[str, _ConnectorState] = {}
        self._events: asyncio.Queue[ConnectorEvent] = asyncio.Queue(maxsize=1024)
        self._stop = asyncio.Event()

    # ----------------------------------------------------------- start/stop

    async def start(self) -> None:
        for c in self.config.connectors:
            self._states[c.name] = _ConnectorState(
                cfg=c,
                is_mcp=(c.type != "local_fs"),
            )
            if c.watch and c.type != "local_fs":
                self._states[c.name].task = asyncio.create_task(
                    self._supervise(c.name),
                    name=f"connector-{c.name}",
                )

    async def stop(self) -> None:
        import contextlib

        self._stop.set()
        tasks = [s.task for s in self._states.values() if s.task]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    # ----------------------------------------------------------- consume

    async def updates(self) -> AsyncIterator[ConnectorEvent]:
        while not self._stop.is_set():
            try:
                yield await asyncio.wait_for(self._events.get(), timeout=1.0)
            except TimeoutError:
                continue

    # ----------------------------------------------------------- one-shot sync

    async def sync_all(self, product_id: str):
        """Yield (source, ResourceRef, content_reader) for every resource across
        all configured connectors. Caller drives the ingest pipeline."""
        for state in self._states.values():
            if state.is_mcp:
                async with McpClientHandle(state.cfg) as handle:
                    refs = await handle.list_resources()
                    state.resources_seen = len(refs)
                    for ref in refs:
                        yield ref, handle.read_resource
            else:
                src = LocalFsSource(LocalFsConfig(root=Path(_extras(state.cfg).get("root", "."))))
                count = 0
                async for ref in src.list_resources():
                    count += 1
                    yield ref, src.read_resource
                state.resources_seen = count

    # ----------------------------------------------------------- supervisor

    async def _supervise(self, name: str) -> None:
        state = self._states[name]
        while not self._stop.is_set():
            try:
                async with McpClientHandle(state.cfg) as handle:
                    state.handle = handle
                    state.backoff_s = 1.0
                    refs = await handle.list_resources()
                    state.resources_seen = len(refs)
                    log.info("connector.%s: connected, %d resources", name, len(refs))
                    # Subscribe to every resource we want to watch.
                    for ref in refs:
                        try:
                            await handle.subscribe(ref.uri)
                        except Exception as e:
                            log.debug("connector.%s: subscribe %s failed: %s", name, ref.uri, e)
                    async for ref in handle.updates():
                        if self._stop.is_set():
                            return
                        await self._events.put(
                            ConnectorEvent(source_id=f"mcp:{name}", resource=ref)
                        )
            except asyncio.CancelledError:
                return
            except Exception as e:
                state.last_error = str(e)
                log.warning(
                    "connector.%s: dropped (%s); reconnecting in %.1fs",
                    name,
                    e,
                    state.backoff_s,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=state.backoff_s)
                    return  # stop set during sleep
                except TimeoutError:
                    pass
                state.backoff_s = min(state.backoff_s * 2.0, 30.0)
            finally:
                state.handle = None

    # ----------------------------------------------------------- status

    def status(self) -> list[dict]:
        out = []
        for state in self._states.values():
            out.append(
                {
                    "name": state.cfg.name,
                    "type": state.cfg.type,
                    "watching": state.cfg.watch,
                    "connected": state.handle is not None,
                    "resources_seen": state.resources_seen,
                    "last_error": state.last_error,
                }
            )
        return out


def _extras(c: ConnectorCfg) -> dict:
    return c.model_dump(exclude={"name", "type", "watch"})
