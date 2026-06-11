"""Skill proposals (council output queue) — see ENGINEERING.md §11."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.api.authz import assert_product_access, filter_products_for_user, rate_limit
from nexus.api.deps import get_config_dep, get_proposal_queue, get_registry
from nexus.config import NexusConfig
from nexus.council.queue import ProposalQueue
from nexus.council.runner import kick_off
from nexus.registry import Registry
from nexus.skills.approval import ApprovalError, ApprovalPublishError, approve_proposal

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
    request: Request,
    queue: ProposalQueue = Depends(get_proposal_queue),
    registry: Registry = Depends(get_registry),
    status_filter: str | None = "pending",
    product_id: str | None = None,
) -> dict:
    if product_id:
        assert_product_access(request, registry, product_id)
        allowed_product_id = product_id
    else:
        allowed = {p["id"] for p in filter_products_for_user(request, registry, registry.list_products())}
        allowed_product_id = None
    proposals = queue.list(status=status_filter, product_id=allowed_product_id)
    if not product_id:
        proposals = [p for p in proposals if p.get("product_id") in allowed]
    return {
        "proposals": proposals
    }


@router.get("/{proposal_id}")
async def get_proposal(
    proposal_id: str,
    request: Request,
    queue: ProposalQueue = Depends(get_proposal_queue),
    registry: Registry = Depends(get_registry),
) -> dict:
    p = queue.get(proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail="proposal not found")
    assert_product_access(request, registry, p["product_id"])
    return p


@router.post("/{proposal_id}/approve")
async def approve(
    proposal_id: str,
    request: Request,
    actor: str = Body(..., embed=True),
    queue: ProposalQueue = Depends(get_proposal_queue),
    config: NexusConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> dict:
    proposal = queue.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    assert_product_access(request, registry, proposal["product_id"], action="approve")
    rate_limit(request, bucket="proposal_approve", limit=60, window_s=86400)
    try:
        return await approve_proposal(
            proposal_id=proposal_id, actor=actor, config=config, queue=queue
        )
    except ApprovalPublishError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ApprovalError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{proposal_id}/edit")
async def edit_proposal(
    proposal_id: str,
    request: Request,
    body: str = Body(..., embed=True),
    actor: str = Body(..., embed=True),
    queue: ProposalQueue = Depends(get_proposal_queue),
    registry: Registry = Depends(get_registry),
) -> dict:
    proposal = queue.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    assert_product_access(request, registry, proposal["product_id"], action="approve")
    if not queue.update_status(proposal_id, status="edited", actor=actor, body=body):
        raise HTTPException(status_code=404, detail="proposal not found")
    queue.record_skill_signal(
        product_id=proposal["product_id"],
        source_type="edit",
        skill_name=proposal.get("name"),
        proposal_id=proposal_id,
        session_id=proposal.get("session_id"),
        text="Human edited proposal body before approval.",
        metadata={"actor": actor},
    )
    return {"ok": True}


@router.post("/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str,
    request: Request,
    body: RejectProposalRequest,
    queue: ProposalQueue = Depends(get_proposal_queue),
    registry: Registry = Depends(get_registry),
) -> dict:
    proposal = queue.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    assert_product_access(request, registry, proposal["product_id"], action="approve")
    if not queue.update_status(proposal_id, status="rejected", actor=None):
        raise HTTPException(status_code=404, detail="proposal not found")
    queue.record_skill_signal(
        product_id=proposal["product_id"],
        source_type="rejection",
        skill_name=proposal.get("name"),
        proposal_id=proposal_id,
        session_id=proposal.get("session_id"),
        text=body.reason,
        metadata={"actor": body.actor, "category": body.category},
    )
    return {"ok": True, "reason": body.reason, "category": body.category}


@router.post("/{proposal_id}/revise")
async def revise_proposal(
    proposal_id: str,
    request: Request,
    body: ReviseProposalRequest,
    queue: ProposalQueue = Depends(get_proposal_queue),
    config: NexusConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> dict:
    proposal = queue.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    assert_product_access(request, registry, proposal["product_id"], action="approve")
    if proposal.get("status") != "pending":
        raise HTTPException(status_code=409, detail="proposal is not pending")

    feedback = body.summary.strip()
    if not feedback:
        raise HTTPException(status_code=422, detail="summary is required")

    if not queue.update_status(
        proposal_id, status="revision_requested", actor=None
    ):
        raise HTTPException(status_code=404, detail="proposal not found")
    queue.record_skill_signal(
        product_id=proposal["product_id"],
        source_type="revision_request",
        skill_name=proposal.get("name"),
        proposal_id=proposal_id,
        session_id=proposal.get("session_id"),
        text=feedback,
        metadata={
            "actor": body.actor,
            "comments": [c.model_dump(mode="json") for c in body.comments],
        },
    )

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
