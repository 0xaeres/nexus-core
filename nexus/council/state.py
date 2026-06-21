"""Council state — flat dict that flows through every LangGraph node.

The planner, experts, synthesizer, repair, eval, and finalizer pass this
TypedDict through LangGraph. Reducers on append-only list fields keep
concurrent expert updates from clobbering each other.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from pydantic import BaseModel, Field

from nexus.skills.models import Critique, EvalStatus, SkillProposal, SkillTier


class EvidenceChunk(BaseModel):
    chunk_id: str
    file: str
    line: int
    score: float
    excerpt: str = ""


class DeliberationMessage(BaseModel):
    agent: str
    timestamp: str  # ISO-8601
    body: str
    cite_ids: list[str] = Field(default_factory=list)


class AgentCost(BaseModel):
    agent: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""


class SkillPlanItem(BaseModel):
    name: str
    description: str = ""
    tier: SkillTier
    purpose: str = ""
    parent: str | None = None
    related: list[str] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)


class ExpertReport(BaseModel):
    expert: str
    summary: str
    findings: list[str] = Field(default_factory=list)
    missing_questions: list[str] = Field(default_factory=list)
    cite_ids: list[str] = Field(default_factory=list)


class SkillDraft(BaseModel):
    name: str
    description: str = ""
    tier: SkillTier
    parent: str | None = None
    related: list[str] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)
    body: str
    repair_attempts: int = 0
    repair_warnings: list[str] = Field(default_factory=list)


class SkillEvalResult(BaseModel):
    skill_name: str
    status: EvalStatus
    summary: str = ""
    failures: list[str] = Field(default_factory=list)
    quality_score: float = Field(ge=0.0, le=1.0, default=0.0)
    attempts: int = 0
    signals_used: list[str] = Field(default_factory=list)


class CouncilState(TypedDict, total=False):
    # Inputs
    session_id: str
    product_id: str
    topic: str
    config_path: str

    # Shared evidence — planner seeds it; expert and repair nodes add bounded context.
    evidence: Annotated[list[EvidenceChunk], operator.add]
    skill_signals: list[dict]
    skill_plan: list[SkillPlanItem]
    expert_reports: Annotated[list[ExpertReport], operator.add]
    skill_drafts: list[SkillDraft]
    eval_results: Annotated[list[SkillEvalResult], operator.add]
    proposals: list[SkillProposal]

    # Per-node outputs
    proposal: SkillProposal | None
    proposal_id: str | None
    critique: Critique | None
    revision_count: int

    # Append-only streams
    deliberation: Annotated[list[DeliberationMessage], operator.add]
    costs: Annotated[list[AgentCost], operator.add]


def initial_state(
    *,
    session_id: str,
    product_id: str,
    topic: str,
    config_path: str,
    skill_signals: list[dict] | None = None,
) -> CouncilState:
    return {
        "session_id": session_id,
        "product_id": product_id,
        "topic": topic,
        "config_path": config_path,
        "evidence": [],
        "skill_signals": skill_signals or [],
        "skill_plan": [],
        "expert_reports": [],
        "skill_drafts": [],
        "eval_results": [],
        "proposals": [],
        "proposal": None,
        "proposal_id": None,
        "critique": None,
        "revision_count": 0,
        "deliberation": [],
        "costs": [],
    }
