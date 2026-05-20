"""Curator - authors Org Library skills with web search + corpus validation.

Workflow:
  1. `web_search(topic + best practices + conventions)` -> external sources.
  2. `hybrid_search` against the in-corpus evidence to validate / ground.
  3. LLM drafts the OrgSkill body + cites both web URLs and in-corpus chunks.

Org Library skills cover tech_stack / language / security and live cross-product.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from nexus.config import NexusConfig
from nexus.council.agents._common import evidence_for_prompt, hits_to_evidence
from nexus.llm.client import ChatClient
from nexus.retrieval.pipeline import RetrievalContext, retrieve
from nexus.skills.models import OrgSkill, OrgSkillKind
from nexus.tools.web_search import WebSearchClient

log = logging.getLogger(__name__)


_SYSTEM = (
    "You are the Curator, an agent of the Nexus LLM Council. You author "
    "ORG-WIDE skills for tech stacks, languages, and security. Sources outside "
    "the codebase are PRIMARY here - cite the URLs you used. The codebase "
    "corpus is SECONDARY validation: only cite a file:line if the corpus shows "
    "an actual usage. Never copy text verbatim from a source."
)


_USER_TEMPLATE = """Topic: {topic}
Kind: {kind}

# External sources (curated web results)
{web_block}

# Corpus validation (in-codebase usages, may be empty)
{corpus_block}

# Task

Author an Org Library skill in Markdown. Structure:

1. `# Title`
2. 2-3 sentence framing
3. `## Rules` (3-6 numbered) - each rule cites at least one external URL.
   Optionally cite `[file: path:line]` when corpus evidence backs it.
4. `## Anti-patterns` (2-4 bullets)

Output ONLY JSON in this schema:

{{
  "name": "kebab-case",
  "body": "markdown body",
  "external_sources": ["https://...", "https://..."],
  "quality_score": 0.0-1.0,
  "applies_to": {{"files": ["**/*.ext"], "contexts": ["code-review"]}}
}}

`quality_score` reflects your self-assessment of how authoritative the
external sources are (peer-reviewed standards > vendor docs > blog posts).
"""


@dataclass
class CuratorResult:
    proposal: OrgSkill
    external_sources: list[str]
    prompt_tokens: int
    completion_tokens: int
    model: str


async def run_curator(
    *,
    topic: str,
    skill_kind: str,
    config: NexusConfig,
    product_for_corpus: str | None = None,
) -> CuratorResult:
    """Self-contained: builds its own retrieval + web search + chat client."""
    if skill_kind not in {k.value for k in OrgSkillKind}:
        raise ValueError(f"curator: unsupported kind {skill_kind!r}")
    kind = OrgSkillKind(skill_kind)

    web = WebSearchClient()
    ctx = RetrievalContext.from_config(config)
    chat = ChatClient.from_cfg(config.models.curator, role="curator")

    try:
        web_query = f"{topic} best practices conventions"
        web_results = await web.search(web_query, max_results=5)

        # Light validation pass against the corpus (any product if not specified).
        evidence_chunks = []
        if product_for_corpus:
            try:
                retrieval = await retrieve(
                    ctx=ctx,
                    product_id=product_for_corpus,
                    query=topic,
                    top_k=10,
                    mode="auto",
                )
                evidence_chunks = hits_to_evidence(retrieval.hits, limit=10)
            except Exception as e:
                log.debug("curator: corpus validation skipped: %s", e)

        web_block = "\n".join(
            f"- [{w.title or w.url}]({w.url})\n  {w.snippet[:240]}"
            for w in web_results
        ) or "_(no external results - offline or no key)_"

        corpus_block = evidence_for_prompt(evidence_chunks) if evidence_chunks else "_(no corpus matches)_"

        payload, usage = await chat.chat_json(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(
                        topic=topic,
                        kind=skill_kind,
                        web_block=web_block,
                        corpus_block=corpus_block,
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=2500,
        )

        from nexus.council.agents.synthesizer import _normalise_name

        name = _normalise_name(payload.get("name") or topic)
        body = str(payload.get("body", "")).strip()
        external_sources = [
            str(u).strip() for u in payload.get("external_sources") or []
        ]
        quality_score = float(payload.get("quality_score", 0.5) or 0.0)
        applies_to_dict = payload.get("applies_to") or {}

        from nexus.skills.models import AppliesTo

        proposal = OrgSkill(
            name=name,
            kind=kind,
            version=1,
            confidence=quality_score,  # mirror until ratification
            quality_score=quality_score,
            external_sources=external_sources or [w.url for w in web_results],
            ratified_by="(pending)",
            ratified_at=datetime.now(UTC).isoformat(),
            applies_to=AppliesTo(
                files=list(applies_to_dict.get("files") or []),
                contexts=list(applies_to_dict.get("contexts") or []),
            ),
            composes_with=[],
            body=body,
        )
        return CuratorResult(
            proposal=proposal,
            external_sources=external_sources,
            prompt_tokens=usage.prompt,
            completion_tokens=usage.completion,
            model=chat.model,
        )
    finally:
        await web.aclose()
        await ctx.aclose()
        await chat.aclose()
