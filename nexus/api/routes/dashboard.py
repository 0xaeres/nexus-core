"""Dashboard aggregate — convenience endpoint for the product home screen."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from nexus.api.deps import get_proposal_queue, get_skill_store
from nexus.council.queue import ProposalQueue
from nexus.skills.models import OrgSkill, Skill
from nexus.skills.store import SkillStore

router = APIRouter(tags=["dashboard"])


@router.get("/products/{product_id}/dashboard")
async def dashboard(
    product_id: str,
    queue: ProposalQueue = Depends(get_proposal_queue),
    store: SkillStore = Depends(get_skill_store),
) -> dict:
    sessions = queue.list_sessions(product_id=product_id)
    pending = queue.list(status="pending", product_id=product_id)
    skills = [
        s
        for s in store.iter_skills()
        if isinstance(s, OrgSkill) or (isinstance(s, Skill) and s.product == product_id)
    ]
    return {
        "daemon": {"state": "idle", "lastEvent": None},
        "pipeline": [
            {"id": "ingest", "label": "Ingestion", "count": 0},
            {"id": "council", "label": "Council", "count": len(sessions)},
            {"id": "skills", "label": "Skills", "count": len(skills)},
            {"id": "pending", "label": "Pending review", "count": len(pending)},
        ],
        "pending": [
            {
                "id": p["id"],
                "kind": p["skill_kind"],
                "label": p["name"],
                "confidence": p["confidence"],
            }
            for p in pending[:10]
        ],
        "recentActivity": [
            {
                "id": s["id"],
                "type": "council",
                "title": f"Council: {s['topic']}",
                "status": s["status"],
                "startedAt": s["started_at"],
                "completedAt": s.get("completed_at"),
            }
            for s in sessions[:10]
        ],
    }
