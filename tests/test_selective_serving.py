"""Tests for MCP selective-serving in nexus.mcp_server.tools.

Verifies that find_skills honors applies_to.files + applies_to.contexts so
the LLM context stays tight.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from nexus.mcp_server.tools import (
    ToolState,
    _matches_context,
    _matches_file_globs,
    corpus_summary,
    find_skills,
    hybrid_search_corpus,
    report_outcome,
)
from nexus.skills.models import AppliesTo, Provenance, Skill
from nexus.skills.store import SkillStore

# ---------- helpers ----------------------------------------------------------


def _state_with_skills(skills: list[Skill]) -> ToolState:
    config = MagicMock()
    state = ToolState(product="test", config=config)
    fake_store = MagicMock(spec=SkillStore)
    fake_store.iter_skills.return_value = skills
    state._store = fake_store
    return state


def _skill(
    name: str,
    *,
    product: str = "test",
    description: str = "",
    tier: str = "domain",
    files: list[str] | None = None,
    contexts: list[str] | None = None,
    body: str = "",
) -> Skill:
    return Skill(
        name=name,
        description=description,
        product=product,
        tier=tier,
        confidence=0.8,
        applies_to=AppliesTo(files=files or [], contexts=contexts or []),
        provenance=Provenance(
            validated_by="t",
            validated_at="2026-05-23T00:00:00Z",
        ),
        body=body or f"# {name}\n\nbody about {name}.",
    )


# ---------- _matches_file_globs ---------------------------------------------


def test_matches_file_globs_empty_means_universal() -> None:
    assert _matches_file_globs("any/path.py", []) is True
    assert _matches_file_globs(None, []) is True


def test_matches_file_globs_no_current_file_passes() -> None:
    assert _matches_file_globs(None, ["**/*.py"]) is True


def test_matches_file_globs_matches_recursive_glob() -> None:
    assert _matches_file_globs("src/app/main.py", ["**/*.py"]) is True
    assert _matches_file_globs("main.py", ["**/*.py"]) is True
    assert _matches_file_globs("src/app/main.py", ["**/*.ts"]) is False


def test_matches_file_globs_any_of_many() -> None:
    globs = ["**/*.ts", "**/*.tsx"]
    assert _matches_file_globs("ui/Foo.tsx", globs) is True
    assert _matches_file_globs("server/main.py", globs) is False


# ---------- _matches_context -------------------------------------------------


def test_matches_context_empty_skill_contexts_passes() -> None:
    assert _matches_context("code-review", []) is True


def test_matches_context_general_is_no_filter() -> None:
    assert _matches_context("general", ["code-review"]) is True
    assert _matches_context("", ["code-review"]) is True


def test_matches_context_requires_exact_membership() -> None:
    assert _matches_context("security-audit", ["code-review"]) is False
    assert _matches_context("security-audit", ["security-audit", "code-review"]) is True


# ---------- find_skills filtering -------------------------------------------


def test_find_skills_current_file_filters_by_globs() -> None:
    py = _skill("python-conventions", files=["**/*.py"])
    ts = _skill("typescript-conventions", files=["**/*.ts", "**/*.tsx"])
    universal = _skill("code-review", files=[], contexts=["code-review"])
    state = _state_with_skills([py, ts, universal])

    result = asyncio.run(
        find_skills(state, query="review this", current_file="src/foo.py")
    )
    ids = [s["id"] for s in result["skills"]]
    assert "test/python-conventions" in ids
    assert "test/typescript-conventions" not in ids
    assert "test/code-review" in ids


def test_find_skills_context_filter_drops_irrelevant() -> None:
    review = _skill("code-review", contexts=["code-review"])
    sec = _skill("security-baseline", contexts=["security-audit"])
    universal = _skill("git-workflow", contexts=[])
    state = _state_with_skills([review, sec, universal])

    result = asyncio.run(find_skills(state, query="audit", context="security-audit"))
    ids = [s["id"] for s in result["skills"]]
    assert "test/security-baseline" in ids
    assert "test/code-review" not in ids
    assert "test/git-workflow" in ids


def test_find_skills_general_context_disables_filter() -> None:
    review = _skill("code-review", contexts=["code-review"])
    sec = _skill("security-baseline", contexts=["security-audit"])
    state = _state_with_skills([review, sec])

    result = asyncio.run(find_skills(state, query="anything", context="general"))
    ids = {s["id"] for s in result["skills"]}
    assert ids == {"test/code-review", "test/security-baseline"}


def test_find_skills_reports_filter_metadata() -> None:
    py = _skill("python-conventions", files=["**/*.py"])
    ts = _skill("typescript-conventions", files=["**/*.ts"])
    state = _state_with_skills([py, ts])
    result = asyncio.run(
        find_skills(state, query="review", current_file="src/foo.py")
    )
    assert result["filtered_from"] == 2
    assert result["current_file"] == "src/foo.py"
    assert len(result["skills"]) == 1


def test_find_skills_includes_master_before_targeted_skills() -> None:
    master = _skill("test-master", tier="product_master", body="# Master\n\nProduct map.")
    py = _skill("python-conventions", files=["**/*.py"])
    state = _state_with_skills([py, master])

    result = asyncio.run(
        find_skills(state, query="review", current_file="src/foo.py")
    )
    assert [s["id"] for s in result["skills"]] == [
        "test/test-master",
        "test/python-conventions",
    ]
    assert result["skills"][0]["tier"] == "product_master"


def test_find_skills_returns_product_skill_first() -> None:
    product = _skill("test-skill", tier="product_master", body="# test-skill\n\nProduct map.")
    legacy = _skill("test-master", tier="product_master", body="# Master\n\nLegacy map.")
    state = _state_with_skills([legacy, product])

    result = asyncio.run(find_skills(state, query="anything"))

    assert [s["id"] for s in result["skills"]][:1] == ["test/test-skill"]


def test_find_skills_ranks_description_matches() -> None:
    auth = _skill(
        "auth",
        description="Use for token rotation and session validation.",
        body="# Auth\n\nNo matching body text.",
    )
    other = _skill("other", description="Use for build tooling.")
    state = _state_with_skills([other, auth])

    result = asyncio.run(find_skills(state, query="token rotation"))
    assert result["skills"][0]["id"] == "test/auth"
    assert result["skills"][0]["summary"] == "Use for token rotation and session validation."


def test_find_skills_is_product_scoped() -> None:
    ours = _skill("ours")
    other = _skill("other", product="other")
    state = _state_with_skills([ours, other])

    result = asyncio.run(find_skills(state, query="anything"))
    assert [s["id"] for s in result["skills"]] == ["test/ours"]
    assert result["filtered_from"] == 1


def test_report_outcome_persists_skill_signal(tmp_path) -> None:
    config = MagicMock()
    config.storage.proposal_queue = tmp_path / "queue.db"
    state = ToolState(product="test", config=config)

    result = asyncio.run(
        report_outcome(
            state,
            skill_name="test-master",
            succeeded=False,
            notes="Skill missed tenancy rules.",
        )
    )

    assert result["ok"] is True
    signals = state.queue.list_skill_signals(product_id="test")
    assert len(signals) == 1
    assert signals[0]["source_type"] == "mcp_outcome"
    assert signals[0]["skill_name"] == "test-master"
    assert signals[0]["text"] == "Skill missed tenancy rules."
    assert signals[0]["metadata"]["succeeded"] is False


def test_hybrid_search_rejects_cross_product_override() -> None:
    state = _state_with_skills([])
    result = asyncio.run(
        hybrid_search_corpus(state, query="auth", product_id="other")
    )
    assert result == {"error": "cross-product corpus search is not allowed"}


def test_corpus_summary_rejects_cross_product_resource() -> None:
    state = _state_with_skills([])
    result = asyncio.run(corpus_summary(state, product_id="other"))
    assert result == {
        "product_id": "other",
        "error": "cross-product corpus access is not allowed",
    }
