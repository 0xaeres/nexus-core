"""Skill proposals (council output queue) — see ENGINEERING.md §11."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from nexus.api.deps import get_config_dep, get_proposal_queue
from nexus.config import NexusConfig
from nexus.council.queue import ProposalQueue
from nexus.council.runner import kick_off
from nexus.skills.approval import ApprovalError, approve_proposal

router = APIRouter(prefix="/proposals", tags=["proposals"])


class RejectProposalRequest(BaseModel):
    reason: str = Field(..., min_length=1)
    category: str | None = None
    actor: str | None = None


class ReviewCommentRequest(BaseModel):
    line: int | None = None
    body: str = Field(..., min_length=1)


class ReviseProposalRequest(BaseModel):
    summary: str = Field(..., min_length=1)
    actor: str | None = None
    comments: list[ReviewCommentRequest] = Field(default_factory=list)


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
    body: RejectProposalRequest,
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    if not queue.update_status(proposal_id, status="rejected", actor=None):
        raise HTTPException(status_code=404, detail="proposal not found")
    return {"ok": True, "reason": body.reason, "category": body.category}


@router.post("/{proposal_id}/revise")
async def revise_proposal(
    proposal_id: str,
    body: ReviseProposalRequest,
    queue: ProposalQueue = Depends(get_proposal_queue),
    config: NexusConfig = Depends(get_config_dep),
) -> dict:
    proposal = queue.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    if proposal.get("status") != "pending":
        raise HTTPException(status_code=409, detail="proposal is not pending")

    feedback = body.summary.strip()
    if not feedback:
        raise HTTPException(status_code=422, detail="summary is required")

    if not queue.update_status(
        proposal_id, status="revision_requested", actor=None
    ):
        raise HTTPException(status_code=404, detail="proposal not found")

    comment_text = "\n".join(
        f"- line {c.line}: {c.body}" if c.line is not None else f"- {c.body}"
        for c in body.comments
    )
    topic = (
        f"Revise skill proposal `{proposal['name']}`.\n\n"
        f"SME requested changes:\n{feedback}\n\n"
        f"Line comments:\n{comment_text or '- none'}\n\n"
        f"Previous draft:\n{proposal['body']}"
    )
    sid = await kick_off(
        config=config,
        queue=queue,
        product_id=proposal["product_id"],
        topic=topic,
    )
    return {"session_id": sid, "status": "running"}
