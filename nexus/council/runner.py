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

from nexus.config import NexusConfig
from nexus.council.errors import CouncilAgentError, CouncilStop
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
        self._live: set[str] = set()
        self._completed: set[str] = set()
        self._lock = asyncio.Lock()

    async def start(self, session_id: str) -> None:
        async with self._lock:
            self._live.add(session_id)
            self._completed.discard(session_id)

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
            self._live.discard(session_id)
            self._completed.add(session_id)
            queues = list(self._subscribers.get(session_id, []))
        for q in queues:
            await q.put(_END)

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2048)
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
        return session_id in self._live and session_id not in self._completed


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
    session_id: str | None = None,
) -> str:
    """Schedule a council run as an asyncio task. Returns the session_id."""
    sid = session_id or make_session_id()
    queue.record_session(
        session_id=sid,
        product_id=product_id,
        topic=topic,
        proposal_id=None,
        proposal_ids=[],
        deliberation=[],
        costs=[],
        started_at=datetime.now(UTC).isoformat(),
        completed_at="",
        status="running",
    )
    await HUB.start(sid)
    task = asyncio.create_task(
        _run_session(
            config=config,
            queue=queue,
            session_id=sid,
            product_id=product_id,
            topic=topic,
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
) -> None:
    started_at = datetime.now(UTC).isoformat()
    deliberation_dumped: list[dict] = []
    costs_dumped: list[dict] = []
    try:
        await HUB.publish(
            session_id,
            {
                "event": "session_start",
                "data": {
                    "session_id": session_id,
                    "product_id": product_id,
                    "topic": topic,
                },
            },
        )
        initial = initial_state(
            session_id=session_id,
            product_id=product_id,
            topic=topic,
            config_path="nexus.yaml",
            skill_signals=queue.list_skill_signals(product_id=product_id, limit=12),
        )
        proposal = None
        proposals = []
        eval_results = []

        async def token_sink(token: dict[str, str]) -> None:
            await HUB.publish(session_id, {"event": "llm_token", "data": token})

        async with (
            council_handles(
                config,
                token_sink=token_sink,
                trace_context={"session_id": session_id, "product_id": product_id},
            ) as handles,
            open_checkpointer(config.storage.council_checkpoint) as saver,
        ):
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
                    if delta.get("proposals"):
                        proposals = list(delta["proposals"])
                    if delta.get("eval_results"):
                        eval_results.extend(delta["eval_results"])

        if proposal is not None or proposals:
            if not proposals and proposal is not None:
                proposals = [proposal]
            if proposal is None:
                proposal = proposals[0]
            proposal_ids = [p.id for p in proposals]
            for item in proposals:
                queue.enqueue(
                    item,
                    session_id=session_id,
                    product_id=product_id,
                    deliberation=deliberation_dumped,
                    costs=costs_dumped,
                )
            _record_eval_results(
                queue=queue,
                session_id=session_id,
                product_id=product_id,
                eval_results=eval_results,
                proposals=proposals,
            )
            queue.record_session(
                session_id=session_id,
                product_id=product_id,
                topic=topic,
                proposal_id=proposal.id,
                proposal_ids=proposal_ids,
                deliberation=deliberation_dumped,
                costs=costs_dumped,
                started_at=started_at,
                completed_at=datetime.now(UTC).isoformat(),
                status="completed",
            )
            await HUB.publish(
                session_id,
                {
                    "event": "proposal",
                    "data": {"proposal_id": proposal.id, "proposal_ids": proposal_ids},
                },
            )
        else:
            _record_eval_results(
                queue=queue,
                session_id=session_id,
                product_id=product_id,
                eval_results=eval_results,
                proposals=[],
            )
            queue.record_session(
                session_id=session_id,
                product_id=product_id,
                topic=topic,
                proposal_id=None,
                proposal_ids=[],
                deliberation=deliberation_dumped,
                costs=costs_dumped,
                started_at=started_at,
                completed_at=datetime.now(UTC).isoformat(),
                status="failed",
            )
            await HUB.publish(session_id, {"event": "error", "data": "no proposal produced"})
    except Exception as e:  # pragma: no cover - defensive
        stopped = _controlled_stop(e)
        if stopped is not None:
            stopped_at = datetime.now(UTC).isoformat()
            notice = _stop_notice(stop=stopped, timestamp=stopped_at)
            log.info(
                "council session %s stopped: %s: %s",
                session_id,
                stopped.reason,
                stopped.detail,
            )
            deliberation_dumped.append(notice["message"])
            _record_eval_results(
                queue=queue,
                session_id=session_id,
                product_id=product_id,
                eval_results=locals().get("eval_results", []),
                proposals=locals().get("proposals", []),
            )
            queue.record_session(
                session_id=session_id,
                product_id=product_id,
                topic=topic,
                proposal_id=None,
                proposal_ids=[],
                deliberation=deliberation_dumped,
                costs=costs_dumped,
                started_at=started_at,
                completed_at=stopped_at,
                status="stopped",
            )
            await HUB.publish(session_id, {"event": "notice", "data": notice["notice"]})
            await HUB.publish(session_id, {"event": "message", "data": notice["message"]})
            return

        log.exception("council session %s crashed", session_id)
        failed_at = datetime.now(UTC).isoformat()
        failure = _failure_message(error=e, timestamp=failed_at)
        deliberation_dumped.append(failure)
        _record_eval_results(
            queue=queue,
            session_id=session_id,
            product_id=product_id,
            eval_results=locals().get("eval_results", []),
            proposals=locals().get("proposals", []),
        )
        queue.record_session(
            session_id=session_id,
            product_id=product_id,
            topic=topic,
            proposal_id=None,
            proposal_ids=[],
            deliberation=deliberation_dumped,
            costs=costs_dumped,
            started_at=started_at,
            completed_at=failed_at,
            status="failed",
        )
        await HUB.publish(session_id, {"event": "message", "data": failure})
        await HUB.publish(
            session_id,
            {
                "event": "error",
                "data": {
                    "message": str(e),
                    "type": type(e).__name__,
                },
            },
        )
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
                    "tier": getattr(proposal, "tier", None),
                    "confidence": proposal.confidence,
                    "eval_status": getattr(proposal, "eval_status", None),
                    "quality_score": getattr(proposal, "quality_score", None),
                    "node": node_name,
                },
            },
        )
    if delta.get("eval_results"):
        for result in delta["eval_results"]:
            await HUB.publish(
                session_id,
                {"event": "skill_eval", "data": _dump(result)},
            )
    if delta.get("proposals"):
        for proposal in delta["proposals"]:
            await HUB.publish(
                session_id,
                {
                    "event": "proposal_preview",
                    "data": {
                        "id": proposal.id,
                        "name": proposal.name,
                        "tier": getattr(proposal, "tier", None),
                        "confidence": proposal.confidence,
                        "eval_status": getattr(proposal, "eval_status", None),
                        "quality_score": getattr(proposal, "quality_score", None),
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


def _record_eval_results(
    *,
    queue: ProposalQueue,
    session_id: str,
    product_id: str,
    eval_results: list,
    proposals: list,
) -> None:
    if not eval_results:
        return
    latest_by_skill = {result.skill_name: result for result in eval_results}
    eval_results = list(latest_by_skill.values())
    by_skill = {p.name: p.id for p in proposals}
    failed = [r for r in eval_results if getattr(r, "status", None) == "failed"]
    run_status = "failed" if len(failed) == len(eval_results) else ("partial" if failed else "passed")
    run_id = f"{session_id}:skill-quality-v1"
    queue.record_eval_run(
        run_id=run_id,
        session_id=session_id,
        product_id=product_id,
        suite_version="skill-quality-v1",
        status=run_status,
        summary=f"{len(eval_results) - len(failed)}/{len(eval_results)} skill eval(s) passed.",
    )
    for result in eval_results:
        failures = list(getattr(result, "failures", []) or [])
        proposal_id = by_skill.get(result.skill_name)
        queue.record_eval_result(
            run_id=run_id,
            session_id=session_id,
            product_id=product_id,
            proposal_id=proposal_id,
            skill_name=result.skill_name,
            status=result.status,
            summary=result.summary,
            failures=failures,
            quality_score=result.quality_score,
            attempts=result.attempts,
            signals_used=list(getattr(result, "signals_used", []) or []),
        )
        if failures:
            queue.record_skill_signal(
                product_id=product_id,
                source_type="eval_failure",
                skill_name=result.skill_name,
                proposal_id=proposal_id,
                session_id=session_id,
                text="\n".join(failures),
                metadata={
                    "summary": result.summary,
                    "quality_score": result.quality_score,
                    "attempts": result.attempts,
                },
            )


def _failure_message(*, error: Exception, timestamp: str) -> dict:
    return {
        "agent": "system",
        "timestamp": timestamp,
        "body": f"Council failed: {type(error).__name__}: {error}",
        "cite_ids": [],
    }


def _controlled_stop(error: Exception) -> CouncilStop | None:
    if isinstance(error, CouncilStop):
        return error
    if isinstance(error, CouncilAgentError) and isinstance(error.cause, CouncilStop):
        return error.cause
    return None


def _stop_notice(*, stop: CouncilStop, timestamp: str) -> dict:
    return {
        "notice": {
            "level": "info",
            "reason": stop.reason,
            "message": stop.user_message,
            "detail": stop.detail,
        },
        "message": {
            "agent": "system",
            "timestamp": timestamp,
            "body": stop.user_message,
            "cite_ids": [],
        },
    }


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
