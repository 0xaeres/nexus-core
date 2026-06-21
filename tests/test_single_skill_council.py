from __future__ import annotations

import pytest

from nexus.config import NexusConfig
from nexus.council.agents import skill
from nexus.council.errors import CouncilNoEvidence
from nexus.council.skill_catalog import catalog_plan
from nexus.council.skill_parser import required_sections_for_tier
from nexus.council.state import CouncilState, EvidenceChunk, SkillDraft, initial_state
from nexus.llm.client import ChatResponse, TokenUsage
from nexus.retrieval.hybrid import Hit
from nexus.retrieval.pipeline import RetrievalResult

_ENUMERABLE = {
    "capabilities and workflows",
    "system map",
    "data model",
    "interfaces and contracts",
    "invariants and constraints",
    "security and secrets",
    "known traps",
    "freshness and evidence",
    "product language",
}


def _config(tmp_path) -> NexusConfig:
    return NexusConfig(
        hierarchy_root=tmp_path / "skills",
        models={
            "council": {"provider": "test", "model": "test"},
            "light": {"provider": "test", "model": "test"},
            "embedding": {"provider": "test", "model": "test", "url": "http://embed"},
            "reranker": {"provider": "test", "model": "test", "url": "http://rerank"},
        },
        storage={
            "proposal_queue": tmp_path / "queue.db",
            "council_checkpoint": tmp_path / "council.sqlite",
        },
    )


class _Chat:
    model = "test"

    async def chat_json(self, *_args, **_kwargs):
        return {
            "summary": "Evidence is sufficient.",
            "findings": ["Product-specific evidence found."],
            "missing_questions": [],
            "missing_evidence": False,
            "questions": [],
        }, TokenUsage(prompt=1, completion=1)

    async def chat_markdown(self, messages, **_kwargs):
        prompt = messages[-1]["content"]
        name = _line_value(prompt, "Skill name")
        tier = _line_value(prompt, "Tier")
        sections = required_sections_for_tier(tier)
        lines = [f"# {name}", ""]
        for title in sections:
            lines.append(f"## {title}")
            if title == "Use This Skill When":
                lines.append(f"Use this {tier} skill for grounded work.")
            elif title.lower() in _ENUMERABLE:
                lines.append("- Follow the fixture convention item one [file: a.py:1].")
                lines.append("- Follow the fixture convention item two [file: a.py:1].")
            elif title in {"Anti-patterns"}:
                lines.append("- Follow the fixture convention [file: a.py:1].")
            else:
                lines.append(f"Fixture evidence supports {title.lower()} [file: a.py:1].")
        return ChatResponse(
            content="\n".join(lines),
            usage=TokenUsage(prompt=1, completion=1),
            model="test",
        )


class _NoEvalChat(_Chat):
    async def chat_json(self, *_args, **_kwargs):
        raise AssertionError("skill_eval must not call chat_json")


@pytest.mark.asyncio
async def test_product_skill_creates_one_proposal_with_fake_retrieval(tmp_path, monkeypatch) -> None:
    async def fake_retrieve(**_kwargs):
        return RetrievalResult(
            hits=[
                Hit(
                    id="c1",
                    score=0.9,
                    payload={
                        "resource_uri": "a.py",
                        "start_line": 1,
                        "content": "architecture API domain pytest config security",
                    },
                    source="dense",
                )
            ],
            seed_count=1,
        )

    monkeypatch.setattr(skill, "retrieve_evidence", fake_retrieve)
    cfg = _config(tmp_path)
    chat = _Chat()
    retrieval = object()
    state: CouncilState = initial_state(
        session_id="cs_1",
        product_id="demo",
        topic="overview",
        config_path="nexus.yaml",
    )

    state.update(await skill.planner(state, config=cfg, retrieval=retrieval, chat=chat))
    state.update(await skill.experts(state, retrieval=retrieval, chat=chat))
    state.update(await skill.synthesizer(state, config=cfg, chat=chat))
    state.update(await skill.repair_loop(state, chat=chat))
    state.update(await skill.evaluator(state, chat=chat))
    state.update(await skill.finalizer(state))

    proposals = state["proposals"]
    assert len(proposals) == 1
    assert proposals[0].name == "demo-skill"
    assert all(p.description for p in proposals)
    assert all(p.citations for p in proposals)
    assert all(p.eval_status == "passed" for p in proposals)
    assert all(p.quality_score > 0 for p in proposals)


@pytest.mark.asyncio
async def test_single_expert_streams_json_and_reports_own_cost(tmp_path, monkeypatch) -> None:
    async def fake_retrieve(**_kwargs):
        return RetrievalResult(
            hits=[
                Hit(
                    id="c1",
                    score=0.9,
                    payload={
                        "resource_uri": "a.py",
                        "start_line": 1,
                        "content": "architecture API domain pytest config security",
                    },
                    source="dense",
                )
            ],
            seed_count=1,
        )

    class ExpertChat(_Chat):
        model = "expert-model"

        def __init__(self):
            self.stream_values: list[bool] = []

        async def chat_json(self, *_args, **kwargs):
            self.stream_values.append(bool(kwargs.get("stream")))
            return await super().chat_json(*_args, **kwargs)

    monkeypatch.setattr(skill, "retrieve_evidence", fake_retrieve)
    chat = ExpertChat()
    state: CouncilState = initial_state(
        session_id="cs_1",
        product_id="demo",
        topic="overview",
        config_path="nexus.yaml",
    )

    result = await skill.expert(
        state,
        name="architect",
        retrieval=object(),
        chat=chat,
    )

    assert chat.stream_values == [True]
    assert result["expert_reports"][0].expert == "architect"
    assert result["deliberation"][0].agent == "architect"
    assert result["costs"][0].agent == "architect"


@pytest.mark.asyncio
async def test_product_skill_eval_is_deterministic_without_model_call() -> None:
    body = _skill_body("demo-skill", "product_master", file="a.py", line=1)
    state: CouncilState = {
        "topic": "overview",
        "product_id": "demo",
        "evidence": [
            EvidenceChunk(
                chunk_id="c1",
                file="a.py",
                line=1,
                score=0.9,
                excerpt="architecture API domain pytest config security",
            )
        ],
        "skill_plan": catalog_plan("demo", "overview"),
        "skill_drafts": [
            SkillDraft(
                name="demo-skill",
                description="Use for product orientation and grounded development.",
                tier="product_master",
                body=body,
            )
        ],
    }

    result = await skill.evaluator(state, chat=_NoEvalChat())

    assert result["eval_results"][0].status == "passed"
    assert result["costs"] == []


@pytest.mark.asyncio
async def test_eval_uses_repair_supplemental_evidence_beyond_initial_cap() -> None:
    evidence = [
        EvidenceChunk(
            chunk_id=f"c{i}",
            file=f"{i}.py",
            line=1,
            score=float(100 - i),
            excerpt="architecture API domain pytest config security",
        )
        for i in range(20)
    ]
    evidence.append(
        EvidenceChunk(
            chunk_id="supplemental",
            file="supplemental.py",
            line=42,
            score=0.1,
            excerpt="supplemental citation anchor",
        )
    )
    body = _skill_body("demo-skill", "product_master", file="supplemental.py", line=42)
    state: CouncilState = {
        "topic": "overview",
        "product_id": "demo",
        "evidence": evidence,
        "skill_plan": catalog_plan("demo", "overview"),
        "skill_drafts": [
            SkillDraft(
                name="demo-skill",
                description="Use for product orientation and grounded development.",
                tier="product_master",
                body=body,
            )
        ],
    }

    result = await skill.evaluator(state, chat=_NoEvalChat())

    assert result["eval_results"][0].status == "passed"
    assert [draft.name for draft in result["skill_drafts"]] == ["demo-skill"]


@pytest.mark.asyncio
async def test_single_skill_no_evidence_stops_before_drafting(tmp_path, monkeypatch) -> None:
    async def fake_retrieve(**_kwargs):
        return RetrievalResult(hits=[], seed_count=0)

    monkeypatch.setattr(skill, "retrieve_evidence", fake_retrieve)
    state = initial_state(
        session_id="cs_1",
        product_id="demo",
        topic="overview",
        config_path="nexus.yaml",
    )

    with pytest.raises(CouncilNoEvidence):
        await skill.planner(state, config=_config(tmp_path), retrieval=object(), chat=_Chat())


@pytest.mark.asyncio
async def test_eval_failure_keeps_passing_skill_and_reports_failed_skill() -> None:
    class RepairStillBadChat(_Chat):
        async def chat_markdown(self, messages, **_kwargs):
            prompt = messages[-1]["content"]
            name = _line_value(prompt, "Skill name")
            tier = _line_value(prompt, "Tier")
            return ChatResponse(
                content=_skill_body(name, tier, file="missing.py", line=99),
                usage=TokenUsage(prompt=1, completion=1),
                model="test",
            )

    evidence = [
        EvidenceChunk(
            chunk_id="c1",
            file="a.py",
            line=1,
            score=0.9,
            excerpt="architecture API domain pytest config security",
        )
    ]
    state: CouncilState = {
        **initial_state(
            session_id="cs_1",
            product_id="demo",
            topic="overview",
            config_path="nexus.yaml",
        ),
        "evidence": evidence,
        "skill_plan": catalog_plan("demo", "overview"),
        "skill_drafts": [
            SkillDraft(
                name="demo-skill",
                description="Use for project overview and grounding.",
                tier="product_master",
                body=_skill_body("demo-skill", "product_master", file="a.py", line=1),
            ),
        ],
    }

    result = await skill.evaluator(state, chat=RepairStillBadChat())

    assert [draft.name for draft in result["skill_drafts"]] == ["demo-skill"]
    statuses = {item.skill_name: item.status for item in result["eval_results"]}
    assert statuses == {"demo-skill": "passed"}


def _line_value(text: str, name: str) -> str:
    prefix = f"{name}: "
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    raise AssertionError(f"missing prompt line: {name}")


def _skill_body(name: str, tier: str, *, file: str, line: int) -> str:
    sections = required_sections_for_tier(tier)
    lines = [f"# {name}", ""]
    for title in sections:
        lines.append(f"## {title}")
        if title == "Use This Skill When":
            lines.append(f"Use this {tier} skill for grounded work.")
        elif title.lower() in _ENUMERABLE:
            lines.append(f"- Fixture item one for {title.lower()} [file: {file}:{line}].")
            lines.append(f"- Fixture item two for {title.lower()} [file: {file}:{line}].")
        elif title in {"Anti-patterns"}:
            lines.append(f"- Follow the fixture convention [file: {file}:{line}].")
        else:
            lines.append(f"Fixture evidence supports {title.lower()} [file: {file}:{line}].")
    return "\n".join(lines)
