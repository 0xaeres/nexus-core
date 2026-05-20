"""Skill proposals (council output queue) — see ENGINEERING.md §11."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from nexus.api.deps import get_config_dep, get_proposal_queue
from nexus.config import NexusConfig
from nexus.council.queue import ProposalQueue
from nexus.skills.approval import ApprovalError, approve_proposal

router = APIRouter(prefix="/proposals", tags=["proposals"])


@router.get("")
async def list_proposals(
    queue: ProposalQueue = Depends(get_proposal_queue),
    status_filter: str | None = "pending",
    product_id: str | None = None,
) -> dict:
    return {
        "proposals": queue.list(status=status_filter, product_id=product_id)
    }


@router.get("/{proposal_id}")
async def get_proposal(
    proposal_id: str, queue: ProposalQueue = Depends(get_proposal_queue)
) -> dict:
    p = queue.get(proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail="proposal not found")
    return p


@router.post("/{proposal_id}/approve")
async def approve(
    proposal_id: str,
    actor: str = Body(..., embed=True),
    queue: ProposalQueue = Depends(get_proposal_queue),
    config: NexusConfig = Depends(get_config_dep),
) -> dict:
    try:
        return await approve_proposal(
            proposal_id=proposal_id, actor=actor, config=config, queue=queue
        )
    except ApprovalError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{proposal_id}/edit")
async def edit_proposal(
    proposal_id: str,
    body: str = Body(..., embed=True),
    actor: str = Body(..., embed=True),
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    if not queue.update_status(proposal_id, status="edited", actor=actor, body=body):
        raise HTTPException(status_code=404, detail="proposal not found")
    return {"ok": True}


@router.post("/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str,
    reason: str,
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    if not queue.update_status(proposal_id, status="rejected", actor=None):
        raise HTTPException(status_code=404, detail="proposal not found")
    return {"ok": True, "reason": reason}
