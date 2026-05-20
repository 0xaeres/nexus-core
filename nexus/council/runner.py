"""In-process council session runner with a live pub/sub for SSE clients.

Background task launches `run_council`, but instead of `ainvoke` we stream node
updates via `astream` and push each delta to a session-specific asyncio.Queue.
Subscribers (e.g. the SSE endpoint) consume from that queue in real time.
Once the run finishes, we persist the proposal + session to SQLite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from nexus.config import NexusConfig
from nexus.council.graph import build_graph, council_handles, open_checkpointer
from nexus.council.queue import ProposalQueue
from nexus.council.state import initial_state

log = logging.getLogger(__name__)

# Sentinel pushed onto a session queue when the run finishes.
_END = "__end__"


class _SessionHub:
    """One asyncio.Queue per session_id; multiple readers fan out via queues."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict | str]]] = {}
        self._completed: set[str] = set()
        self._lock = asyncio.Lock()

    async def publish(self, session_id: str, event: dict) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(session_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("session %s: subscriber queue full; dropping event", session_id)

    async def finish(self, session_id: str) -> None:
        async with self._lock:
            self._completed.add(session_id)
            queues = list(self._subscribers.get(session_id, []))
        for q in queues:
            await q.put(_END)

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.setdefault(session_id, []).append(q)
            if session_id in self._completed:
                # session finished before subscriber arrived; signal immediately
                await q.put(_END)
        return q

    async def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        async with self._lock:
            queues = self._subscribers.get(session_id)
            if queues and q in queues:
                queues.remove(q)
            if not queues:
                self._subscribers.pop(session_id, None)

    def is_live(self, session_id: str) -> bool:
        return (
            session_id in self._subscribers
            and session_id not in self._completed
        )


HUB = _SessionHub()

# Background council tasks are anchored here so the GC does not cancel them
# mid-flight (asyncio.create_task only holds a weak reference).
_RUNNING: set[asyncio.Task] = set()


# ---------------------------------------------------------------- kickoff


def make_session_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"cs_{ts}_{uuid.uuid4().hex[:6]}"


async def kick_off(
    *,
    config: NexusConfig,
    queue: ProposalQueue,
    product_id: str,
    topic: str,
    skill_kind: str,
    session_id: str | None = None,
) -> str:
    """Schedule a council run as an asyncio task. Returns the session_id."""
    sid = session_id or make_session_id()
    task = asyncio.create_task(
        _run_session(
            config=config,
            queue=queue,
            session_id=sid,
            product_id=product_id,
            topic=topic,
            skill_kind=skill_kind,
        ),
        name=f"council-{sid}",
    )
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return sid


async def _run_session(
    *,
    config: NexusConfig,
    queue: ProposalQueue,
    session_id: str,
    product_id: str,
    topic: str,
    skill_kind: str,
) -> None:
    started_at = datetime.now(UTC).isoformat()
    try:
        await HUB.publish(
            session_id,
            {
                "event": "session_start",
                "data": {
                    "session_id": session_id,
                    "product_id": product_id,
                    "topic": topic,
                    "skill_kind": skill_kind,
                },
            },
        )
        initial = initial_state(
            session_id=session_id,
            product_id=product_id,
            topic=topic,
            skill_kind=skill_kind,
            config_path="nexus.yaml",
        )
        deliberation_dumped: list[dict] = []
        costs_dumped: list[dict] = []
        proposal = None

        async with council_handles(config) as handles:
            with open_checkpointer(config.storage.council_checkpoint) as saver:
                graph = build_graph(config, handles)
                compiled = graph.compile(checkpointer=saver)
                async for node_update in compiled.astream(
                    initial,
                    config={"configurable": {"thread_id": session_id}},
                ):
                    # `node_update` is `{node_name: partial_state}` per LangGraph
                    for node_name, delta in (node_update or {}).items():
                        await _publish_node_delta(session_id, node_name, delta)
                        for m in delta.get("deliberation", []) or []:
                            deliberation_dumped.append(_dump(m))
                        for c in delta.get("costs", []) or []:
                            costs_dumped.append(_dump(c))
                        if "proposal" in delta and delta["proposal"] is not None:
                            proposal = delta["proposal"]

        if proposal is not None:
            queue.enqueue(
                proposal,
                session_id=session_id,
                product_id=product_id,
                skill_kind=skill_kind,
                deliberation=deliberation_dumped,
                costs=costs_dumped,
            )
            queue.record_session(
                session_id=session_id,
                product_id=product_id,
                skill_kind=skill_kind,
                topic=topic,
                proposal_id=proposal.id,
                deliberation=deliberation_dumped,
                costs=costs_dumped,
                started_at=started_at,
                completed_at=datetime.now(UTC).isoformat(),
                status="completed",
            )
            await HUB.publish(
                session_id,
                {"event": "proposal", "data": {"proposal_id": proposal.id}},
            )
        else:
            queue.record_session(
                session_id=session_id,
                product_id=product_id,
                skill_kind=skill_kind,
                topic=topic,
                proposal_id=None,
                deliberation=deliberation_dumped,
                costs=costs_dumped,
                started_at=started_at,
                completed_at=datetime.now(UTC).isoformat(),
                status="failed",
            )
            await HUB.publish(session_id, {"event": "error", "data": "no proposal produced"})
    except Exception as e:  # pragma: no cover - defensive
        log.exception("council session %s crashed", session_id)
        await HUB.publish(session_id, {"event": "error", "data": str(e)})
    finally:
        await HUB.publish(session_id, {"event": "session_end", "data": {}})
        await HUB.finish(session_id)


async def _publish_node_delta(session_id: str, node_name: str, delta: dict) -> None:
    """Translate one LangGraph node update into a set of SSE-shaped events."""
    if not delta:
        return
    for m in delta.get("deliberation", []) or []:
        await HUB.publish(session_id, {"event": "message", "data": _dump(m)})
    for c in delta.get("costs", []) or []:
        await HUB.publish(session_id, {"event": "cost", "data": _dump(c)})
    if "critique" in delta and delta["critique"] is not None:
        await HUB.publish(
            session_id,
            {"event": "critique", "data": _dump(delta["critique"])},
        )
    if "proposal" in delta and delta["proposal"] is not None:
        # Lightweight proposal preview during streaming; full record persists at end.
        proposal = delta["proposal"]
        await HUB.publish(
            session_id,
            {
                "event": "proposal_preview",
                "data": {
                    "id": proposal.id,
                    "name": proposal.name,
                    "confidence": proposal.confidence,
                    "node": node_name,
                },
            },
        )


def _dump(obj) -> dict:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return obj
    return {"value": str(obj)}


# ---------------------------------------------------------------- consumer


async def stream_events(session_id: str) -> AsyncIterator[dict]:
    """Yield SSE-shaped events for one session. Caller is the SSE endpoint."""
    q = await HUB.subscribe(session_id)
    try:
        while True:
            item = await q.get()
            if item == _END:
                return
            assert isinstance(item, dict)
            yield {"event": item.get("event", "message"), "data": json.dumps(item.get("data", {}))}
    finally:
        await HUB.unsubscribe(session_id, q)


# convenience for callers needing pathlib
_PathlibPath = Path
