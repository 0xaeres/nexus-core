"""Council state — flat dict that flows through every LangGraph node.

Three nodes touch this state: Drafter, Critic, Reviser. Each appends to the
same TypedDict; reducers on the append-only list fields keep concurrent updates
from clobbering each other.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from pydantic import BaseModel, Field

from nexus.skills.models import Critique, SkillProposal, SkillTier


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
    tier: SkillTier
    parent: str | None = None
    related: list[str] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)
    body: str
    repair_attempts: int = 0


class JudgeResult(BaseModel):
    passed: bool = True
    missing_evidence: bool = False
    questions: list[str] = Field(default_factory=list)
    summary: str = ""


class CouncilState(TypedDict, total=False):
    # Inputs
    session_id: str
    product_id: str
    topic: str
    config_path: str

    # Shared evidence — populated by Drafter; Critic adds its own re-retrieval to it.
    evidence: Annotated[list[EvidenceChunk], operator.add]
    skill_plan: list[SkillPlanItem]
    expert_reports: Annotated[list[ExpertReport], operator.add]
    skill_drafts: list[SkillDraft]
    proposals: list[SkillProposal]
    judge_result: JudgeResult | None
    callback_count: int

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
) -> CouncilState:
    return {
        "session_id": session_id,
        "product_id": product_id,
        "topic": topic,
        "config_path": config_path,
        "evidence": [],
        "skill_plan": [],
        "expert_reports": [],
        "skill_drafts": [],
        "proposals": [],
        "judge_result": None,
        "callback_count": 0,
        "proposal": None,
        "proposal_id": None,
        "critique": None,
        "revision_count": 0,
        "deliberation": [],
        "costs": [],
    }
