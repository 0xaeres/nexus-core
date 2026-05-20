"""Org Library - shared standards across products. See ENGINEERING.md §11."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException

from nexus.api.deps import get_config_dep, get_proposal_queue
from nexus.config import NexusConfig
from nexus.council.agents.curator import run_curator
from nexus.council.change_request import review as review_change_request
from nexus.council.queue import OrgProposalQueue, ProposalQueue
from nexus.skills.models import AppliesTo, OrgSkill, OrgSkillKind
from nexus.skills.store import SkillStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/org", tags=["org-library"])


_RUNNING: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


def _org_queue(queue: ProposalQueue) -> OrgProposalQueue:
    """Reuse the same DB file."""
    return OrgProposalQueue(queue.db_path)


def _org_store(config: NexusConfig) -> SkillStore:
    root = Path(config.org_library_root)
    if not root.is_absolute():
        root = Path.cwd() / root
    return SkillStore(root)


# ---------------------------------------------------------------- skills


@router.get("/skills")
async def list_org_skills(config: NexusConfig = Depends(get_config_dep)) -> dict:
    store = _org_store(config)
    skills = [s for s in store.iter_skills() if isinstance(s, OrgSkill)]
    return {
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "kind": s.kind.value,
                "scope": "org",
                "version": s.version,
                "quality_score": s.quality_score,
                "external_sources": s.external_sources,
                "ratified_by": s.ratified_by,
                "ratified_at": s.ratified_at,
                "applies_to": s.applies_to.model_dump(),
                "body": s.body,
            }
            for s in skills
        ]
    }


@router.get("/skills/{skill_id:path}")
async def get_org_skill(
    skill_id: str,
    queue: ProposalQueue = Depends(get_proposal_queue),
    config: NexusConfig = Depends(get_config_dep),
) -> dict:
    store = _org_store(config)
    target = skill_id.split("/")[-1]
    skill = next(
        (
            s
            for s in store.iter_skills()
            if isinstance(s, OrgSkill) and (s.id == skill_id or s.name == target)
        ),
        None,
    )
    if not skill:
        raise HTTPException(status_code=404, detail="org skill not found")
    crs = _org_queue(queue).list_change_requests(org_skill_id=skill.id)
    return {
        "skill": skill.model_dump(mode="json"),
        "changeRequests": crs,
    }


# ---------------------------------------------------------------- Curator


@router.post("/skills")
async def kick_off_curator(
    topic: str = Body(..., embed=True),
    kind: str = Body(..., embed=True),
    product_for_corpus: str | None = Body(None, embed=True),
    queue: ProposalQueue = Depends(get_proposal_queue),
    config: NexusConfig = Depends(get_config_dep),
) -> dict:
    if kind not in {k.value for k in OrgSkillKind}:
        raise HTTPException(status_code=400, detail=f"unsupported kind {kind!r}")
    proposal_id = "orgp_" + uuid.uuid4().hex[:12]
    _spawn(
        _curator_task(
            proposal_id=proposal_id,
            topic=topic,
            kind=kind,
            product_for_corpus=product_for_corpus,
            queue=queue,
            config=config,
        )
    )
    return {"proposal_id": proposal_id, "status": "running"}


async def _curator_task(
    *,
    proposal_id: str,
    topic: str,
    kind: str,
    product_for_corpus: str | None,
    queue: ProposalQueue,
    config: NexusConfig,
) -> None:
    org_q = _org_queue(queue)
    try:
        result = await run_curator(
            topic=topic,
            skill_kind=kind,
            config=config,
            product_for_corpus=product_for_corpus,
        )
        proposal = result.proposal
        org_q.enqueue_org_proposal(
            proposal_id=proposal_id,
            name=proposal.name,
            kind=proposal.kind.value,
            body=proposal.body,
            quality_score=proposal.quality_score,
            external_sources=proposal.external_sources,
            applies_to=proposal.applies_to.model_dump(),
        )
        log.info("curator: stored %s (%s)", proposal_id, proposal.name)
    except Exception:
        log.exception("curator: failed for %s", proposal_id)


@router.get("/proposals")
async def list_org_proposals(
    queue: ProposalQueue = Depends(get_proposal_queue),
    status: str = "pending",
) -> dict:
    return {"proposals": _org_queue(queue).list_org_proposals(status=status)}


@router.post("/skills/{proposal_id}/ratify")
async def ratify_org_skill(
    proposal_id: str,
    actor: str = Body(..., embed=True),
    queue: ProposalQueue = Depends(get_proposal_queue),
    config: NexusConfig = Depends(get_config_dep),
) -> dict:
    org_q = _org_queue(queue)
    proposal = org_q.get_org_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    if proposal["status"] != "pending":
        return {
            "ok": True,
            "skipped": "already_decided",
            "skill_id": f"org/{proposal['name']}",
        }

    kind = OrgSkillKind(proposal["kind"])
    skill = OrgSkill(
        name=proposal["name"],
        kind=kind,
        version=1,
        confidence=float(proposal["quality_score"]),
        quality_score=float(proposal["quality_score"]),
        external_sources=proposal["external_sources"],
        ratified_by=actor,
        ratified_at=proposal["created_at"],
        applies_to=AppliesTo(**proposal["applies_to"]),
        composes_with=[],
        body=proposal["body"],
    )
    store = _org_store(config)
    path = store.save(skill, SkillStore.relative_path_for(skill))
    org_q.ratify_org_proposal(proposal_id, actor=actor)
    return {"ok": True, "skill_id": skill.id, "path": str(path)}


# ---------------------------------------------------------------- change requests


@router.post("/skills/{skill_id:path}/change-requests")
async def file_change_request(
    skill_id: str,
    title: str = Body(..., embed=True),
    proposed_diff: str = Body(..., embed=True),
    rationale: str = Body(..., embed=True),
    requester: str = Body(..., embed=True),
    queue: ProposalQueue = Depends(get_proposal_queue),
    config: NexusConfig = Depends(get_config_dep),
) -> dict:
    store = _org_store(config)
    target = skill_id.split("/")[-1]
    skill = next(
        (
            s
            for s in store.iter_skills()
            if isinstance(s, OrgSkill) and (s.id == skill_id or s.name == target)
        ),
        None,
    )
    if not skill:
        raise HTTPException(status_code=404, detail="org skill not found")

    request_id = "cr_" + uuid.uuid4().hex[:12]
    org_q = _org_queue(queue)
    org_q.file_change_request(
        request_id=request_id,
        org_skill_id=skill.id,
        skill_kind=skill.kind.value,
        title=title,
        proposed_diff=proposed_diff,
        rationale=rationale,
        requested_by=requester,
    )
    _spawn(
        _cr_review_task(
            request_id=request_id,
            skill_name=skill.name,
            skill_kind=skill.kind.value,
            current_body=skill.body,
            diff=proposed_diff,
            rationale=rationale,
            queue=queue,
            config=config,
        )
    )
    return {"request_id": request_id, "status": "filed"}


async def _cr_review_task(
    *,
    request_id: str,
    skill_name: str,
    skill_kind: str,
    current_body: str,
    diff: str,
    rationale: str,
    queue: ProposalQueue,
    config: NexusConfig,
) -> None:
    try:
        verdict = await review_change_request(
            skill_name=skill_name,
            skill_kind=skill_kind,
            current_body=current_body,
            diff=diff,
            rationale=rationale,
            config=config,
        )
        _org_queue(queue).attach_agent_verdict(
            request_id, agent_verdict=verdict.to_dict()
        )
        log.info(
            "cr_review: %s -> %s (%s)",
            request_id,
            verdict.verdict,
            verdict.agent,
        )
    except Exception:
        log.exception("cr_review: failed for %s", request_id)


@router.post("/skills/{skill_id:path}/change-requests/{request_id}/approve")
async def approve_change_request(
    skill_id: str,
    request_id: str,
    actor: str = Body(..., embed=True),
    queue: ProposalQueue = Depends(get_proposal_queue),
    config: NexusConfig = Depends(get_config_dep),
) -> dict:
    org_q = _org_queue(queue)
    cr = org_q.get_change_request(request_id)
    if not cr:
        raise HTTPException(status_code=404, detail="change request not found")
    if not org_q.decide_change_request(request_id, outcome="approved", actor=actor):
        raise HTTPException(status_code=500, detail="failed to update CR")

    # Cache invalidation: best-effort purge for the org skill body chunk.
    try:
        from nexus.ingest.indexer import Indexer
        from nexus.retrieval.cache import SemanticCache

        indexer = Indexer(url=config.vector_store.url)
        cache = SemanticCache(
            client=indexer.client,
            threshold=config.cache.semantic_threshold,
            ttl_s=config.cache.ttl_hours * 3600,
        )
        # Purge any cache entry tagged with this skill's chunk ids by deleting
        # all cache rows for the affected product(s) and chunk_ids found via
        # `delete_by_resource(resource_uri startswith skill path)`.
        # Cheap MVP: purge by ignoring chunk_ids - rely on TTL.
        log.info("approve_change_request: cache purge via TTL (skill=%s)", skill_id)
        await indexer.aclose()
        _ = cache  # silence unused
    except Exception as e:
        log.debug("cache invalidation soft-failed: %s", e)
    return {"ok": True, "verdict": "approved"}


@router.post("/skills/{skill_id:path}/change-requests/{request_id}/reject")
async def reject_change_request(
    skill_id: str,
    request_id: str,
    actor: str = Body(..., embed=True),
    reason: str = Body(..., embed=True),
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    org_q = _org_queue(queue)
    cr = org_q.get_change_request(request_id)
    if not cr:
        raise HTTPException(status_code=404, detail="change request not found")
    if not org_q.decide_change_request(
        request_id, outcome="rejected", actor=actor, reason=reason
    ):
        raise HTTPException(status_code=500, detail="failed to update CR")
    return {"ok": True, "verdict": "rejected", "reason": reason}
