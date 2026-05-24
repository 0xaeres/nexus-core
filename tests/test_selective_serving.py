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
    find_skills,
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
    files: list[str] | None = None,
    contexts: list[str] | None = None,
    body: str = "",
) -> Skill:
    return Skill(
        name=name,
        product=product,
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
