"""Skills — see ENGINEERING.md §8."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from nexus.api.deps import get_proposal_queue, get_skill_store
from nexus.council.queue import ProposalQueue
from nexus.skills.store import SkillStore

router = APIRouter(tags=["skills"])


@router.get("/products/{product_id}/skills")
async def list_product_skills(
    product_id: str, store: SkillStore = Depends(get_skill_store)
) -> dict:
    skills: list[dict] = []
    grouped: dict[str, list[dict]] = {}
    for s in store.iter_skills():
        if s.product != product_id:
            continue
        d = s.model_dump(mode="json")
        d["id"] = s.id
        skills.append(d)
        grouped.setdefault(s.tier, []).append(d)
    return {"skills": skills, "grouped": grouped}


def _find_skill(store: SkillStore, skill_id: str):
    target_name = skill_id.split("/")[-1]
    for s in store.iter_skills():
        if s.id == skill_id or s.name == target_name:
            return s
    return None


@router.get("/skills/{skill_id:path}/corrections")
async def get_skill_corrections(
    skill_id: str,
    store: SkillStore = Depends(get_skill_store),
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    skill = _find_skill(store, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")

    proposals = queue.list(product_id=skill.product)
    approved = [p for p in proposals if p.get("status") == "approved"]
    corrections = []
    for p in approved:
        critique = p.get("adversary_critique")
        if critique:
            corrections.append({
                "proposal_id": p["id"],
                "created_at": p.get("created_at"),
                "adversary_critique": critique,
            })

    built_in = getattr(getattr(skill, "provenance", None), "adversary_critique", None)
    return {"corrections": corrections, "adversary_critique": built_in}


@router.get("/skills/{skill_id:path}/rejections")
async def get_skill_rejections(
    skill_id: str,
    store: SkillStore = Depends(get_skill_store),
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    skill = _find_skill(store, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")

    rejections = queue.list(status="rejected", product_id=skill.product)
    return {"rejections": rejections}


@router.get("/skills/{skill_id:path}/council-history")
async def get_skill_council_history(
    skill_id: str,
    store: SkillStore = Depends(get_skill_store),
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    skill = _find_skill(store, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")

    sessions = queue.list_sessions(product_id=skill.product)
    return {"sessions": sessions}


@router.get("/skills/{skill_id:path}")
async def get_skill(
    skill_id: str, store: SkillStore = Depends(get_skill_store)
) -> dict:
    skill = _find_skill(store, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    d = skill.model_dump(mode="json")
    d["id"] = skill.id
    return d
