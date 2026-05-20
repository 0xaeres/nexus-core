"""Skills — see ENGINEERING.md §11."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from nexus.api.deps import get_skill_store
from nexus.skills.models import OrgSkill, Skill
from nexus.skills.store import SkillStore

router = APIRouter(tags=["skills"])


@router.get("/products/{product_id}/skills")
async def list_product_skills(
    product_id: str, store: SkillStore = Depends(get_skill_store)
) -> dict:
    out_master: dict | None = None
    out_domain: list[dict] = []
    out_adopted: list[dict] = []
    for s in store.iter_skills():
        d = s.model_dump(mode="json")
        d["id"] = s.id
        if isinstance(s, OrgSkill):
            out_adopted.append(d)
        else:
            assert isinstance(s, Skill)
            if s.product != product_id:
                continue
            if str(s.kind) == "master":
                out_master = d
            else:
                out_domain.append(d)
    return {
        "master": out_master,
        "domain": out_domain,
        "adopted": out_adopted,
    }


@router.get("/skills/{skill_id:path}")
async def get_skill(
    skill_id: str, store: SkillStore = Depends(get_skill_store)
) -> dict:
    target_name = skill_id.split("/")[-1]
    for s in store.iter_skills():
        if s.id == skill_id or s.name == target_name:
            d = s.model_dump(mode="json")
            d["id"] = s.id
            return d
    raise HTTPException(status_code=404, detail="skill not found")
