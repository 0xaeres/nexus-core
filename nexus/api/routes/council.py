"""Council sessions + live/replay SSE — see ENGINEERING.md §11."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from nexus.api.authz import assert_product_access, auth_enabled, rate_limit
from nexus.api.deps import get_config_dep, get_proposal_queue, get_registry
from nexus.config import NexusConfig
from nexus.council.queue import ProposalQueue
from nexus.council.runner import HUB, kick_off, stream_events
from nexus.registry import Registry

router = APIRouter(tags=["council"])


@router.get("/products/{product_id}/council/sessions")
async def list_sessions(
    product_id: str,
    request: Request,
    queue: ProposalQueue = Depends(get_proposal_queue),
    registry: Registry = Depends(get_registry),
) -> dict:
    assert_product_access(request, registry, product_id)
    return {"sessions": queue.list_sessions(product_id=product_id)}


@router.post("/products/{product_id}/council/sessions")
async def create_session(
    product_id: str,
    request: Request,
    topic: str = Body(..., embed=True),
    queue: ProposalQueue = Depends(get_proposal_queue),
    config: NexusConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> dict:
    """Schedule a council run as a background task. Returns the session_id."""
    assert_product_access(request, registry, product_id, action="council")
    rate_limit(request, bucket="council_start", limit=30, window_s=86400)
    sid = await kick_off(
        config=config, queue=queue, product_id=product_id, topic=topic
    )
    return {"session_id": sid, "status": "running"}


@router.get("/council/sessions/{session_id}/stream")
async def session_stream(
    session_id: str,
    request: Request,
    queue: ProposalQueue = Depends(get_proposal_queue),
    registry: Registry = Depends(get_registry),
) -> EventSourceResponse:
    """Live stream if the session is running; replay if it's already complete."""
    sess = queue.get_session(session_id)
    if not sess:
        if HUB.is_live(session_id) and not auth_enabled():
            return EventSourceResponse(stream_events(session_id))
        raise HTTPException(status_code=404, detail="session not found")
    assert_product_access(request, registry, sess["product_id"])
    if HUB.is_live(session_id):
        return EventSourceResponse(stream_events(session_id))

    async def replay() -> AsyncIterator[dict]:
        yield {
            "event": "session_start",
            "data": json.dumps(
                {
                    "session_id": session_id,
                    "topic": sess.get("topic"),
                    "replay": True,
                }
            ),
        }
        for msg in sess.get("deliberation", []):
            yield {"event": "message", "data": json.dumps(msg)}
        for result in sess.get("eval_results", []):
            yield {"event": "skill_eval", "data": json.dumps(result)}
        if sess.get("status") == "stopped":
            message = "Council stopped before producing a proposal."
            deliberation = sess.get("deliberation", [])
            if deliberation:
                message = deliberation[-1].get("body") or message
            yield {
                "event": "notice",
                "data": json.dumps(
                    {
                        "level": "info",
                        "reason": "stopped",
                        "message": message,
                    }
                ),
            }
        for cost in sess.get("costs", []):
            yield {"event": "cost", "data": json.dumps(cost)}
        if sess.get("proposal_id"):
            yield {
                "event": "proposal",
                "data": json.dumps(
                    {
                        "proposal_id": sess["proposal_id"],
                        "proposal_ids": sess.get("proposal_ids") or [],
                    }
                ),
            }
        yield {"event": "session_end", "data": "{}"}

    return EventSourceResponse(replay())


@router.get("/council/sessions/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    queue: ProposalQueue = Depends(get_proposal_queue),
    registry: Registry = Depends(get_registry),
) -> dict:
    sess = queue.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    assert_product_access(request, registry, sess["product_id"])
    return sess
