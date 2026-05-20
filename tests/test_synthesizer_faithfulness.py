from nexus.council.agents.synthesizer import _normalise_name, _strip_uncited_assertions
from nexus.council.state import EvidenceChunk


def _evi() -> list[EvidenceChunk]:
    return [
        EvidenceChunk(chunk_id="c1", file="a.rs", line=10, score=0.9, excerpt="x"),
        EvidenceChunk(chunk_id="c2", file="b.rs", line=20, score=0.8, excerpt="y"),
    ]


def test_strip_keeps_cited_rule_items() -> None:
    body = (
        "# Title\n\nIntro paragraph.\n\n"
        "## Rules\n\n"
        "1. Cited rule [file: a.rs:10].\n"
        "2. Another cited rule [file: b.rs:20].\n"
    )
    out, dropped = _strip_uncited_assertions(body, _evi())
    assert dropped == 0
    assert out == body


def test_strip_removes_uncited_rule_items() -> None:
    body = (
        "# Title\n\n"
        "## Rules\n\n"
        "1. Cited rule [file: a.rs:10].\n"
        "2. Uncited claim without anchor.\n"
        "3. Another cited rule [file: b.rs:20].\n"
    )
    out, dropped = _strip_uncited_assertions(body, _evi())
    assert dropped == 1
    assert "Uncited claim" not in out
    assert "Cited rule" in out


def test_strip_leaves_prose_outside_rules_alone() -> None:
    body = (
        "# Title\n\nFree-form intro with no citation. Still fine.\n\n"
        "## Rules\n\n"
        "1. Cited [file: a.rs:10].\n\n"
        "## Anti-patterns\n\n"
        "- An uncited anti-pattern, allowed in this section.\n"
    )
    out, dropped = _strip_uncited_assertions(body, _evi())
    assert dropped == 0
    assert "Free-form intro" in out
    assert "uncited anti-pattern" in out


def test_normalise_name_kebab_cases_and_caps_length() -> None:
    assert _normalise_name("PDA Seed Validation") == "pda-seed-validation"
    assert _normalise_name("snake_case_input") == "snake-case-input"
    assert _normalise_name("--leading--and--trailing--") == "leading-and-trailing"
    long_name = "x" * 200
    assert len(_normalise_name(long_name)) <= 60
