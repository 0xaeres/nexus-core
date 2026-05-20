"""Change-request routing - selects the agent for an Org Library diff review.

Rules per spec §6.4:
  security             -> Security Sentinel
  tech_stack | language -> Archaeologist (light review, no Sentinel needed)
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Literal

from nexus.config import NexusConfig
from nexus.council.agents import security_sentinel
from nexus.llm.client import ChatClient

log = logging.getLogger(__name__)


@dataclass
class AgentVerdict:
    agent: str
    verdict: Literal["low_risk", "medium_risk", "high_risk"]
    analysis: str
    recommendation: str

    def to_dict(self) -> dict:
        return asdict(self)


def route(skill_kind: str) -> str:
    """Return the agent name responsible for reviewing this kind."""
    if skill_kind == "security":
        return "security_sentinel"
    if skill_kind in {"tech_stack", "language"}:
        return "archaeologist"
    # product_domain / master live outside the Org Library workflow but
    # are routed defensively.
    return "archaeologist"


async def review(
    *,
    skill_name: str,
    skill_kind: str,
    current_body: str,
    diff: str,
    rationale: str,
    config: NexusConfig,
) -> AgentVerdict:
    agent = route(skill_kind)
    if agent == "security_sentinel":
        a = await security_sentinel.review_change_request(
            skill_name=skill_name,
            current_body=current_body,
            diff=diff,
            rationale=rationale,
            config=config,
        )
        return AgentVerdict(
            agent="security_sentinel",
            verdict=a.verdict,
            analysis=a.analysis,
            recommendation=a.recommendation,
        )
    # Light Archaeologist pass for tech_stack / language
    return await _archaeologist_review(
        skill_name=skill_name,
        current_body=current_body,
        diff=diff,
        rationale=rationale,
        config=config,
    )


_ARCH_SYSTEM = (
    "You are the Archaeologist reviewing a change request to an ORG-WIDE skill. "
    "Judge whether the diff is consistent with current ecosystem conventions. "
    "Less paranoid than the Security Sentinel - default to low_risk for typos, "
    "wording tweaks, doc adds. Escalate to medium_risk for behavioural changes."
)


async def _archaeologist_review(
    *,
    skill_name: str,
    current_body: str,
    diff: str,
    rationale: str,
    config: NexusConfig,
) -> AgentVerdict:
    chat = ChatClient.from_cfg(
        config.models.council_agents, role="archaeologist:cr_review"
    )
    try:
        prompt = (
            f"Skill: {skill_name}\n\n"
            f"# Current body\n{current_body[:4000]}\n\n"
            f"# Diff\n{diff[:4000]}\n\n"
            f"# Rationale\n{rationale[:1500]}\n\n"
            'Output JSON: {"verdict": "low_risk"|"medium_risk"|"high_risk", '
            '"analysis": "...", "recommendation": "..."}'
        )
        payload, _ = await chat.chat_json(
            [
                {"role": "system", "content": _ARCH_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=800,
        )
        verdict = str(payload.get("verdict", "low_risk")).lower()
        if verdict not in ("low_risk", "medium_risk", "high_risk"):
            verdict = "low_risk"
        return AgentVerdict(
            agent="archaeologist",
            verdict=verdict,  # type: ignore[arg-type]
            analysis=str(payload.get("analysis", "")).strip(),
            recommendation=str(payload.get("recommendation", "")).strip(),
        )
    finally:
        await chat.aclose()
