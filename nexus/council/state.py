"""Council state — single Pydantic model that flows through every LangGraph node.

Each agent appends to the same dict-shaped state. Reducers (`add_messages`-style
list appends) are configured on each list field so concurrent updates from
fan-out nodes don't clobber each other.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

from nexus.skills.models import Critique, SkillProposal

# ---------------------------------------------------------------- agent outputs


class EvidenceChunk(BaseModel):
    chunk_id: str
    file: str
    line: int
    score: float
    excerpt: str = ""


class CodePattern(BaseModel):
    name: str
    description: str
    evidence: list[EvidenceChunk] = Field(default_factory=list)


class CodePatterns(BaseModel):
    """Output of the Archaeologist agent."""

    patterns: list[CodePattern]
    notes: str = ""


class DomainContext(BaseModel):
    """Output of the Domain Expert agent."""

    vocabulary: list[str] = Field(default_factory=list)
    entity_relationships: list[str] = Field(default_factory=list)
    summary: str = ""
    evidence: list[EvidenceChunk] = Field(default_factory=list)


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


# ---------------------------------------------------------------- state


class CouncilState(TypedDict, total=False):
    """LangGraph state. Each field has a reducer so fan-out updates merge cleanly."""

    # Inputs
    session_id: str
    product_id: str
    topic: str
    skill_kind: Literal["master", "product_domain"]
    config_path: str

    # Agent outputs (set by individual nodes)
    code_patterns: CodePatterns | None
    domain_context: DomainContext | None
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
    skill_kind: str,
    config_path: str,
) -> CouncilState:
    return {
        "session_id": session_id,
        "product_id": product_id,
        "topic": topic,
        "skill_kind": skill_kind,  # type: ignore[typeddict-item]
        "config_path": config_path,
        "code_patterns": None,
        "domain_context": None,
        "proposal": None,
        "proposal_id": None,
        "critique": None,
        "revision_count": 0,
        "deliberation": [],
        "costs": [],
    }
