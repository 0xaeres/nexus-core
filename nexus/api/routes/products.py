"""Products + auth — see ENGINEERING.md §8."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from nexus.api.authz import (
    assert_product_access,
    auth_enabled,
    filter_products_for_user,
    product_permissions,
    public_user,
    rate_limit,
    require_user,
)
from nexus.api.deps import get_proposal_queue, get_registry, get_skill_store
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.skills.store import SkillStore

router = APIRouter(tags=["products"])


@router.get("/me")
async def me(request: Request, registry: Registry = Depends(get_registry)) -> dict:
    auth_user = getattr(request.state, "user", None)
    if auth_user is not None:
        memberships = registry.list_product_memberships(auth_user["id"])
        role = next(iter(memberships.values()), None)
        return {
            "user": public_user(auth_user, registry),
            "permissions": product_permissions(auth_user, role),
            "memberships": memberships,
        }

    if auth_enabled():
        raise HTTPException(status_code=401, detail="authentication required")

    # Single dev user until deployed auth is enabled.
    user = registry.get_user("admin")
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
    request: Request,
    registry: Registry = Depends(get_registry),
    queue: ProposalQueue = Depends(get_proposal_queue),
    store: SkillStore = Depends(get_skill_store),
) -> dict:
    products = filter_products_for_user(request, registry, registry.list_products())
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
    product_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> dict:
    assert_product_access(request, registry, product_id)
    p = registry.get_product(product_id)
    if not p:
        raise HTTPException(status_code=404, detail="product not found")
    return p


# Live stages, ordered from latest to earliest.
_TERMINAL_SESSION_STATUSES = {"completed", "failed", "stopped"}


@router.get("/products/{product_id}/status")
async def get_product_status(
    product_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    queue: ProposalQueue = Depends(get_proposal_queue),
    store: SkillStore = Depends(get_skill_store),
) -> dict:
    """Single cheap call that powers project-card state in the dashboard.

    Stage precedence (highest wins): review > skill > council > ingesting > none.
    The `councilInProgress` flag is independent of stage so the UI can render
    "Run Council" vs "Council in progress" at the same stage.
    """
    if not registry.get_product(product_id):
        raise HTTPException(status_code=404, detail="product not found")
    assert_product_access(request, registry, product_id)

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

    if has_pending:
        stage = "review"
    elif has_skill:
        stage = "skill"
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
    request: Request,
    id: str = Body(..., embed=True),
    name: str = Body(..., embed=True),
    tagline: str = Body("", embed=True),
    owner: dict = Body(default_factory=dict, embed=True),
    registry: Registry = Depends(get_registry),
) -> dict:
    user = require_user(request)
    rate_limit(request, bucket="product_create", limit=20, window_s=86400)
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
    if user.get("role") != "admin":
        registry.grant_product_role(id, user["id"], "owner")
    return registry.get_product(id)
