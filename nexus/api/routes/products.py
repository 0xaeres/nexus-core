"""Products + auth — see ENGINEERING.md §11."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException

from nexus.api.deps import get_proposal_queue, get_registry, get_skill_store
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.skills.models import OrgSkill
from nexus.skills.store import SkillStore

router = APIRouter(tags=["products"])


@router.get("/me")
async def me(registry: Registry = Depends(get_registry)) -> dict:
    # Single static user for Slice 4. RBAC arrives later.
    user = registry.get_user("jl")
    if not user:
        raise HTTPException(status_code=404, detail="current user not provisioned")
    return {
        "user": user,
        "permissions": {
            "canManageSources": True,
            "canRunCouncil": True,
            "canOnboard": True,
            "isOrgAdmin": False,
            "settingsReadOnly": False,
        },
    }


@router.get("/products")
async def list_products(
    registry: Registry = Depends(get_registry),
    queue: ProposalQueue = Depends(get_proposal_queue),
    store: SkillStore = Depends(get_skill_store),
) -> dict:
    products = registry.list_products()
    enriched: list[dict] = []
    for p in products:
        sessions = queue.list_sessions(product_id=p["id"])
        skills_count = sum(
            1
            for s in store.iter_skills()
            if not isinstance(s, OrgSkill) and (s.product == p["id"])
        )
        enriched.append(
            {
                **p,
                "sources": 0,  # populated by sources endpoint; cheap counter for now
                "skills": skills_count,
                "lastCouncil": (sessions[0]["completed_at"] if sessions else None),
            }
        )
    return {"products": enriched}


@router.get("/products/{product_id}")
async def get_product(
    product_id: str, registry: Registry = Depends(get_registry)
) -> dict:
    p = registry.get_product(product_id)
    if not p:
        raise HTTPException(status_code=404, detail="product not found")
    return p


@router.post("/products")
async def create_product(
    id: str = Body(..., embed=True),
    name: str = Body(..., embed=True),
    tagline: str = Body("", embed=True),
    owner: dict = Body(default_factory=dict, embed=True),
    registry: Registry = Depends(get_registry),
) -> dict:
    if not id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="product id must be alphanumeric / - / _")
    existing = registry.get_product(id)
    if existing:
        raise HTTPException(status_code=409, detail=f"product {id!r} already exists")
    registry.upsert_product({
        "id": id,
        "name": name,
        "tagline": tagline,
        "owner": owner,
        "onboardedAt": datetime.now(UTC).isoformat(),
        "masterSkillId": id,
    })
    return registry.get_product(id)


@router.get("/products/{product_id}/settings")
async def get_product_settings(
    product_id: str,
    registry: Registry = Depends(get_registry),
) -> dict:
    """Members + the model assignments visible to product admins."""
    from nexus.config import get_config

    p = registry.get_product(product_id)
    if not p:
        raise HTTPException(status_code=404, detail="product not found")
    members = [
        u for u in registry.list_users() if product_id in (u.get("products") or [])
    ]
    cfg = get_config()
    models = {
        "council_agents": cfg.models.council_agents.model_dump(exclude={"api_key"}),
        "synthesizer": cfg.models.synthesizer.model_dump(exclude={"api_key"}),
        "adversary": cfg.models.adversary.model_dump(exclude={"api_key"}),
        "pr_review": cfg.models.pr_review.model_dump(exclude={"api_key"}),
        "changelog": cfg.models.changelog.model_dump(exclude={"api_key"}),
        "curator": cfg.models.curator.model_dump(exclude={"api_key"}),
        "light": cfg.models.light.model_dump(exclude={"api_key"}),
        "embedding": cfg.models.embedding.model_dump(exclude={"api_key"}),
        "reranker": cfg.models.reranker.model_dump(exclude={"api_key"}),
    }
    return {"product": p, "members": members, "models": models}


@router.get("/settings/org")
async def get_org_settings(registry: Registry = Depends(get_registry)) -> dict:
    """Org-wide: list all users + a billing placeholder."""
    return {
        "admins": [u for u in registry.list_users() if u.get("role") == "org_admin"],
        "members": registry.list_users(),
        "billing": {"placeholder": True},
    }
