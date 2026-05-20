"""Security Sentinel - reviews change requests against security skills.

Input: current OrgSkill body + proposed diff + rationale.
Output: verdict (low_risk | medium_risk | high_risk), analysis, recommendation.

A tiny built-in CVE / OWASP rule pack is used as soft anchors in the system
prompt; rules grow as we observe pain points. Real CVE feed integration is
deferred.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from nexus.config import NexusConfig
from nexus.llm.client import ChatClient

log = logging.getLogger(__name__)


Verdict = Literal["low_risk", "medium_risk", "high_risk"]


@dataclass
class SecurityAnalysis:
    verdict: Verdict
    analysis: str
    recommendation: str
    prompt_tokens: int
    completion_tokens: int
    model: str


_RULE_PACK = """
Soft anchors (non-exhaustive):
- OWASP Top 10 (2021): A01 broken access control, A02 cryptographic failures,
  A03 injection, A04 insecure design, A05 misconfiguration, A06 vulnerable
  components, A07 ident/auth failures, A08 software/data integrity, A09
  logging failures, A10 SSRF.
- Common CVE classes: deserialisation, command injection, path traversal,
  XXE, SSRF, ReDoS, prototype pollution.
- Validate at boundary; sanitise at sink. Allow-list, not deny-list.
"""


_SYSTEM = (
    "You are the Security Sentinel. You review proposed diffs to ORG-WIDE "
    "security skills. Be skeptical: relaxations are high-risk; tightenings "
    "are usually fine. Cite specific OWASP/CWE classes where the diff "
    "intersects them. NEVER auto-approve."
)


_USER_TEMPLATE = """Skill: {skill_name}

# Current rules
{current_body}

# Proposed diff
{diff}

# Requester rationale
{rationale}

{rule_pack}

# Task

Decide verdict (low_risk | medium_risk | high_risk) and explain. Output
ONLY JSON in this schema:

{{
  "verdict": "low_risk" | "medium_risk" | "high_risk",
  "analysis": "2-4 sentences describing concrete risks (cite OWASP/CWE)",
  "recommendation": "1-2 sentences for the org admin"
}}

Heuristic anchors:
- Any rule *relaxation* without compensating control -> medium_risk minimum.
- Removing an allow-list, weakening crypto, dropping logging -> high_risk.
- Adding a stricter rule, tightening allow-lists, new logging -> low_risk.
"""


async def review_change_request(
    *,
    skill_name: str,
    current_body: str,
    diff: str,
    rationale: str,
    config: NexusConfig,
) -> SecurityAnalysis:
    chat = ChatClient.from_cfg(config.models.synthesizer, role="security_sentinel")
    try:
        payload, usage = await chat.chat_json(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(
                        skill_name=skill_name,
                        current_body=current_body[:4000],
                        diff=diff[:4000],
                        rationale=rationale[:1500],
                        rule_pack=_RULE_PACK.strip(),
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=1000,
        )
        verdict = str(payload.get("verdict", "medium_risk")).lower()
        if verdict not in ("low_risk", "medium_risk", "high_risk"):
            verdict = "medium_risk"
        return SecurityAnalysis(
            verdict=verdict,  # type: ignore[arg-type]
            analysis=str(payload.get("analysis", "")).strip(),
            recommendation=str(payload.get("recommendation", "")).strip(),
            prompt_tokens=usage.prompt,
            completion_tokens=usage.completion,
            model=chat.model,
        )
    finally:
        await chat.aclose()
