"""MCP client wrapper - one subprocess per connector.

Spawns an MCP server over stdio, initialises a `ClientSession`, exposes
`list_resources` / `read_resource` / `subscribe_updates` as a small async API.
Notifications are pushed to an async queue so callers iterate cleanly.

Type -> command mapping handles a few common SaaS connectors out of the box;
'custom' lets nexus.yaml supply any command.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from nexus.config import ConnectorCfg
from nexus.ingest.models import ResourceRef, guess_mime

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- type -> command


def _command_for(c: ConnectorCfg) -> StdioServerParameters | None:
    """Resolve a `ConnectorCfg` into a StdioServerParameters.

    Returns None for non-MCP sources (e.g. local_fs) that the manager handles
    via its in-process fallback path.
    """
    extras = c.model_dump(exclude={"name", "type", "watch"})

    if c.type == "github":
        token = extras.get("token") or os.environ.get("GITHUB_TOKEN", "")
        return StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={**os.environ, "GITHUB_PERSONAL_ACCESS_TOKEN": token},
        )
    if c.type == "filesystem":
        roots = extras.get("roots") or [os.getcwd()]
        return StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", *roots],
        )
    if c.type == "custom":
        cmd = extras.get("command")
        if not cmd or not isinstance(cmd, list):
            raise ValueError(f"custom connector {c.name}: 'command' (list) required")
        return StdioServerParameters(
            command=cmd[0],
            args=list(cmd[1:]),
            env=os.environ.copy(),
        )
    if c.type == "local_fs":
        return None  # handled by manager's in-process LocalFsSource path

    raise ValueError(f"unsupported connector type: {c.type!r}")


# ---------------------------------------------------------------- client


@dataclass
class _RawResource:
    uri: str
    mime: str
    name: str | None = None


class McpClientHandle:
    """One MCP subprocess + session. Use as async context manager."""

    def __init__(self, cfg: ConnectorCfg):
        self.cfg = cfg
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)

    async def __aenter__(self) -> McpClientHandle:
        params = _command_for(self.cfg)
        if params is None:
            raise ValueError(
                f"connector {self.cfg.name} has type {self.cfg.type!r}; "
                "not an MCP server (use LocalFsSource directly)"
            )

        async def _on_notification(msg) -> None:
            try:
                self._notifications.put_nowait(_extract_notification(msg))
            except asyncio.QueueFull:
                log.warning("mcp.%s: notification queue full; dropping", self.cfg.name)

        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(
            ClientSession(read, write, message_handler=_on_notification)
        )
        await session.initialize()
        self._session = session
        log.info("mcp.%s: session ready", self.cfg.name)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()
        self._session = None

    # ----------------------------------------------------------- ops

    async def list_resources(self) -> list[ResourceRef]:
        assert self._session is not None
        result = await self._session.list_resources()
        out: list[ResourceRef] = []
        for r in result.resources:
            uri = str(r.uri)
            mime = r.mimeType or guess_mime(uri)
            out.append(
                ResourceRef(source_id=f"mcp:{self.cfg.name}", uri=uri, mime=mime)
            )
        return out

    async def read_resource(self, uri: str) -> str:
        assert self._session is not None
        result = await self._session.read_resource(uri)  # type: ignore[arg-type]
        # MCP returns either text or blob contents; we want text only here.
        for content in result.contents:
            text = getattr(content, "text", None)
            if text:
                return text
        return ""

    async def subscribe(self, uri: str) -> None:
        assert self._session is not None
        await self._session.subscribe_resource(uri)  # type: ignore[arg-type]

    async def updates(self) -> AsyncIterator[ResourceRef]:
        """Yield `ResourceRef` for each `resources/updated` notification."""
        while True:
            note = await self._notifications.get()
            uri = note.get("uri")
            if uri:
                yield ResourceRef(
                    source_id=f"mcp:{self.cfg.name}",
                    uri=uri,
                    mime=guess_mime(uri),
                )


# ---------------------------------------------------------------- helpers


def _extract_notification(msg) -> dict[str, Any]:
    """Pull a {method, uri} dict from whatever the SDK passed us.

    The exact shape varies across MCP SDK versions; defensive coding here.
    """
    method = getattr(msg, "method", None) or _digv(msg, "method")
    params = getattr(msg, "params", None) or _digv(msg, "params") or {}
    if hasattr(params, "model_dump"):
        params = params.model_dump()
    uri = params.get("uri") if isinstance(params, dict) else None
    return {"method": str(method or ""), "uri": uri, "params": params}


def _digv(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    if hasattr(obj, key):
        return getattr(obj, key)
    return None
