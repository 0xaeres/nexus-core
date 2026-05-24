"""Products + auth — see ENGINEERING.md §11."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException

from nexus.api.deps import get_proposal_queue, get_registry, get_skill_store
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
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
        skills_count = sum(1 for s in store.iter_skills() if s.product == p["id"])
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


# Live stages, ordered from latest to earliest.
_TERMINAL_SESSION_STATUSES = {"completed", "failed"}


@router.get("/products/{product_id}/status")
async def get_product_status(
    product_id: str,
    registry: Registry = Depends(get_registry),
    queue: ProposalQueue = Depends(get_proposal_queue),
    store: SkillStore = Depends(get_skill_store),
) -> dict:
    """Single cheap call that powers project-card state in the dashboard.

    Stage precedence (highest wins): skill > review > council > ingesting > none.
    The `councilInProgress` flag is independent of stage so the UI can render
    "Run Council" vs "Council in progress" at the same stage.
    """
    if not registry.get_product(product_id):
        raise HTTPException(status_code=404, detail="product not found")

    sources = registry.list_sources(product_id)
    has_sources = bool(sources)
    has_embeddings = any(
        s.get("lastSync") and int(s.get("resourceCount") or 0) > 0 for s in sources
    )

    has_skill = any(s.product == product_id for s in store.iter_skills())

    pending = queue.list(status="pending", product_id=product_id)
    has_pending = bool(pending)

    sessions = queue.list_sessions(product_id=product_id)
    live = next(
        (s for s in sessions if s["status"] not in _TERMINAL_SESSION_STATUSES),
        None,
    )

    if has_skill:
        stage = "skill"
    elif has_pending:
        stage = "review"
    elif has_embeddings:
        stage = "council"
    elif has_sources:
        stage = "ingesting"
    else:
        stage = "none"

    return {
        "hasEmbeddings": has_embeddings,
        "hasSkill": has_skill,
        "councilInProgress": live is not None,
        "currentSessionId": (live["id"] if live else None),
        "currentStage": stage,
    }


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
    })
    return registry.get_product(id)



