from pathlib import Path

from evals.common import GoldenItem, load_golden


def test_load_golden_parses_jsonl_file() -> None:
    path = Path(__file__).resolve().parent.parent / "evals" / "golden.jsonl"
    items = load_golden(path)
    assert len(items) >= 25
    # All ids are unique
    assert len({i.id for i in items}) == len(items)
    # All items declare a complexity
    assert all(i.complexity in ("simple", "complex") for i in items)


def test_golden_items_cover_all_seed_skills() -> None:
    path = Path(__file__).resolve().parent.parent / "evals" / "golden.jsonl"
    skills = {i.expected_skill for i in load_golden(path) if i.expected_skill}
    assert "pda-seed-validation" in skills
    assert "swap-fee-math" in skills
    assert "owasp-input-validation" in skills
    assert "typescript-conventions" in skills


def test_golden_has_at_least_one_pairwise_pair() -> None:
    path = Path(__file__).resolve().parent.parent / "evals" / "golden.jsonl"
    pairs = [i for i in load_golden(path) if i.anti_answer]
    assert len(pairs) >= 1


def test_from_dict_handles_minimal_payload() -> None:
    item = GoldenItem.from_dict({"id": "x", "query": "what?"})
    assert item.id == "x"
    assert item.query == "what?"
    assert item.expected_files == []
    assert item.complexity == "complex"
