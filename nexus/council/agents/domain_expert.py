"""Domain Expert — extracts product domain vocabulary + relationships from docs.

Workflow: text-mode retrieval (top-30) → ask the LLM for vocabulary, entity
relationships, and a short summary. Product-scoped retrieval only (no web).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from nexus.config import NexusConfig
from nexus.council.agents._common import (
    evidence_for_prompt,
    hits_to_evidence,
    retrieval_unavailable,
)
from nexus.council.state import (
    AgentCost,
    CouncilState,
    DeliberationMessage,
    DomainContext,
    EvidenceChunk,
)
from nexus.llm.client import ChatClient
from nexus.retrieval.pipeline import RetrievalContext, retrieve

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are the Domain Expert, an agent of the Nexus LLM Council. You analyse "
    "product documentation, ADRs, and design notes — never code — to extract "
    "the vocabulary and conceptual relationships specific to a product. Stay "
    "strictly within the corpus you are shown."
)


_USER_TEMPLATE = """Topic: {topic}
Skill kind: {skill_kind}
Product: {product_id}

Below is the retrieved documentation evidence. Each excerpt is labelled
[E1], [E2], etc.

{evidence}

Output ONLY JSON in this schema:

{{
  "vocabulary": ["term1", "term2", ...],
  "entity_relationships": [
    "Service A owns Service B",
    "Module X supersedes Module Y"
  ],
  "summary": "1-3 sentence summary of the product context relevant to the topic"
}}

Use at most 12 vocabulary items and 6 relationships. Be precise and avoid
generic terms (e.g. 'application', 'system').
"""


async def run(
    state: CouncilState,
    *,
    config: NexusConfig,
    retrieval: RetrievalContext,
    chat: ChatClient,
) -> dict:
    topic = state["topic"]
    product_id = state["product_id"]

    result = await retrieve(
        ctx=retrieval,
        product_id=product_id,
        query=topic,
        top_k=30,
        mode="text",
        context_hint="domain-expert",
    )
    evidence_chunks = hits_to_evidence(result.hits, limit=20)
    unavail = retrieval_unavailable(result)
    if unavail:
        log.warning("domain_expert: %s", unavail)
        return _empty_update(unavail, evidence_chunks, chat.model)

    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _USER_TEMPLATE.format(
                topic=topic,
                skill_kind=state["skill_kind"],
                product_id=product_id,
                evidence=evidence_for_prompt(evidence_chunks),
            ),
        },
    ]
    payload, usage = await chat.chat_json(messages)
    ctx = DomainContext(
        vocabulary=[str(v).strip() for v in payload.get("vocabulary", [])][:12],
        entity_relationships=[
            str(r).strip() for r in payload.get("entity_relationships", [])
        ][:6],
        summary=str(payload.get("summary", "")).strip(),
        evidence=evidence_chunks,
    )

    return {
        "domain_context": ctx,
        "deliberation": [
            DeliberationMessage(
                agent="domain_expert",
                timestamp=datetime.now(UTC).isoformat(),
                body=_render_summary(ctx),
                cite_ids=[e.chunk_id for e in evidence_chunks[:8]],
            )
        ],
        "costs": [
            AgentCost(
                agent="domain_expert",
                prompt_tokens=usage.prompt,
                completion_tokens=usage.completion,
                model=chat.model,
            )
        ],
    }


def _render_summary(ctx: DomainContext) -> str:
    parts = [ctx.summary] if ctx.summary else []
    if ctx.vocabulary:
        parts.append(f"Vocabulary ({len(ctx.vocabulary)}): {', '.join(ctx.vocabulary)}")
    if ctx.entity_relationships:
        parts.append(
            "Relationships:\n" + "\n".join(f"  - {r}" for r in ctx.entity_relationships)
        )
    return "\n\n".join(parts) or "No domain context extracted."


def _empty_update(reason: str, evidence: list[EvidenceChunk], model: str) -> dict:
    return {
        "domain_context": DomainContext(evidence=evidence, summary=reason),
        "deliberation": [
            DeliberationMessage(
                agent="domain_expert",
                timestamp=datetime.now(UTC).isoformat(),
                body=f"Could not extract context: {reason}",
                cite_ids=[e.chunk_id for e in evidence[:4]],
            )
        ],
        "costs": [AgentCost(agent="domain_expert", model=model)],
    }
