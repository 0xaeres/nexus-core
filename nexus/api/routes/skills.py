"""Skills — see ENGINEERING.md §8."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from nexus.api.authz import assert_product_access, auth_enabled, current_user
from nexus.api.deps import get_proposal_queue, get_registry, get_skill_store
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.skills.store import SkillStore

router = APIRouter(tags=["skills"])


@router.get("/products/{product_id}/skills")
async def list_product_skills(
    product_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    store: SkillStore = Depends(get_skill_store),
) -> dict:
    assert_product_access(request, registry, product_id)
    skills: list[dict] = []
    grouped: dict[str, list[dict]] = {}
    for s in store.iter_skills():
        if s.product != product_id:
            continue
        d = s.model_dump(mode="json")
        d["id"] = s.id
        skills.append(d)
        grouped.setdefault(s.tier, []).append(d)
    skills.sort(key=lambda s: (0 if s.get("tier") == "product_master" else 1, s.get("name", "")))
    for items in grouped.values():
        items.sort(key=lambda s: (0 if s.get("tier") == "product_master" else 1, s.get("name", "")))
    return {"skills": skills, "grouped": grouped}


def _accessible_products(request: Request, registry: Registry) -> set[str] | None:
    if not auth_enabled():
        return None
    user = current_user(request)
    if user.get("role") == "admin":
        return None
    return set(registry.list_product_ids_for_user(str(user["id"])))


def _find_skill(
    store: SkillStore,
    skill_id: str,
    *,
    products: set[str] | None = None,
):
    if "/" in skill_id:
        for s in store.iter_skills():
            if s.id == skill_id and (products is None or s.product in products):
                return s
        return None

    target_name = skill_id.split("/")[-1]
    matches = []
    for s in store.iter_skills():
        if s.name == target_name and (products is None or s.product in products):
            matches.append(s)
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="ambiguous skill name; use product/name")
    return matches[0] if matches else None


@router.get("/skills/{skill_id:path}/corrections")
async def get_skill_corrections(
    skill_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    store: SkillStore = Depends(get_skill_store),
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    skill = _find_skill(store, skill_id, products=_accessible_products(request, registry))
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    assert_product_access(request, registry, skill.product)

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
    request: Request,
    registry: Registry = Depends(get_registry),
    store: SkillStore = Depends(get_skill_store),
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    skill = _find_skill(store, skill_id, products=_accessible_products(request, registry))
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    assert_product_access(request, registry, skill.product)

    rejections = queue.list(status="rejected", product_id=skill.product)
    return {"rejections": rejections}


@router.get("/skills/{skill_id:path}/council-history")
async def get_skill_council_history(
    skill_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    store: SkillStore = Depends(get_skill_store),
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    skill = _find_skill(store, skill_id, products=_accessible_products(request, registry))
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    assert_product_access(request, registry, skill.product)

    sessions = queue.list_sessions(product_id=skill.product)
    return {"sessions": sessions}


@router.get("/skills/{skill_id:path}/quality")
async def get_skill_quality(
    skill_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    store: SkillStore = Depends(get_skill_store),
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    skill = _find_skill(store, skill_id, products=_accessible_products(request, registry))
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    assert_product_access(request, registry, skill.product)

    eval_results = queue.list_eval_results(
        product_id=skill.product,
        skill_name=skill.name,
        limit=20,
    )
    signals = queue.list_skill_signals(
        product_id=skill.product,
        skill_name=skill.name,
        limit=20,
    )
    failures = [r for r in eval_results if r.get("status") == "failed"]
    return {
        "skill_id": skill.id,
        "latest_eval": eval_results[0] if eval_results else None,
        "eval_results": eval_results,
        "signals": signals,
        "regeneration_recommended": bool(failures or signals),
    }


@router.get("/skills/{skill_id:path}")
async def get_skill(
    skill_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    store: SkillStore = Depends(get_skill_store),
) -> dict:
    skill = _find_skill(store, skill_id, products=_accessible_products(request, registry))
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    assert_product_access(request, registry, skill.product)
    d = skill.model_dump(mode="json")
    d["id"] = skill.id
    return d
