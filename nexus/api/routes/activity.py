"""Activity timeline — see ENGINEERING.md §11."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from nexus.api.deps import get_proposal_queue
from nexus.council.queue import ProposalQueue

router = APIRouter(prefix="/products/{product_id}/activity", tags=["activity"])


@router.get("")
async def list_activity(
    product_id: str, queue: ProposalQueue = Depends(get_proposal_queue)
) -> dict:
    """Council sessions for now; ingest + PR-review events join in Slice 5."""
    activity = []
    for s in queue.list_sessions(product_id=product_id):
        activity.append(
            {
                "id": s["id"],
                "product": product_id,
                "type": "council",
                "title": f"Council: {s['topic']}",
                "status": "completed" if s["status"] == "completed" else "failed",
                "startedAt": s["started_at"],
                "completedAt": s.get("completed_at"),
            }
        )
    return {"activity": activity}
