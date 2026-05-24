from pathlib import Path

from nexus.skills.models import AppliesTo, Provenance, Skill
from nexus.skills.store import SkillStore


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    skill = Skill(
        name="example",
        product="forge",
        version=2,
        confidence=0.5,
        applies_to=AppliesTo(files=["**/*.py"], contexts=["code-review"]),
        provenance=Provenance(
            validated_by="me@example.com",
            validated_at="2026-05-18T00:00:00Z",
            evidence_chunks=["c1", "c2"],
            revision_count=1,
        ),
        body="# Example\n\nBody here.\n",
    )
    path = store.save(skill)
    assert path.exists()
    assert path.name == "example.skill.md"
    assert path.parent.name == "forge"

    loaded = store.load_path(path)
    assert loaded.name == "example"
    assert loaded.product == "forge"
    assert loaded.confidence == 0.5
    assert loaded.applies_to.files == ["**/*.py"]
    assert loaded.body.strip() == "# Example\n\nBody here.".strip()
    assert loaded.provenance.revision_count == 1


def test_iter_skips_legacy_files_without_product(tmp_path: Path) -> None:
    """Legacy org-library files (no `product:` in frontmatter) are skipped."""
    legacy = tmp_path / "shared" / "legacy.skill.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "---\nname: legacy\nkind: language\nscope: org\n---\n# Legacy\n",
        encoding="utf-8",
    )
    store = SkillStore(tmp_path)
    assert store.iter_skills() == []
