from pathlib import Path

from nexus.skills.models import (
    AppliesTo,
    OrgSkill,
    OrgSkillKind,
    Provenance,
    Skill,
    SkillKind,
    SkillScope,
)
from nexus.skills.store import SkillStore


def test_load_seed_skills_from_disk() -> None:
    root = Path(__file__).resolve().parent.parent / "nexus" / "skills" / "seed"
    store = SkillStore(root)
    skills = store.iter_skills()
    assert len(skills) >= 4
    by_name = {s.name: s for s in skills}
    assert "forge" in by_name
    assert "pda-seed-validation" in by_name
    assert "owasp-input-validation" in by_name


def test_master_seed_shape() -> None:
    root = Path(__file__).resolve().parent.parent / "nexus" / "skills" / "seed"
    store = SkillStore(root)
    skill = next(s for s in store.iter_skills() if s.name == "forge")
    assert isinstance(skill, Skill)
    assert skill.kind is SkillKind.MASTER
    assert skill.product == "forge"
    assert "Master Skill" in skill.body
    assert skill.confidence > 0


def test_org_skill_loads_with_org_scope() -> None:
    root = Path(__file__).resolve().parent.parent / "nexus" / "skills" / "seed"
    store = SkillStore(root)
    skill = next(s for s in store.iter_skills() if s.name == "owasp-input-validation")
    assert isinstance(skill, OrgSkill)
    assert skill.kind is OrgSkillKind.SECURITY
    assert skill.ratified_by


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    skill = Skill(
        name="example",
        kind=SkillKind.PRODUCT_DOMAIN,
        scope=SkillScope.PRODUCT,
        product="forge",
        version=2,
        confidence=0.5,
        applies_to=AppliesTo(files=["**/*.py"], contexts=["code-review"]),
        composes_with=["master"],
        provenance=Provenance(
            validated_by="me@example.com",
            validated_at="2026-05-18T00:00:00Z",
            evidence_chunks=["c1", "c2"],
            revision_count=1,
        ),
        body="# Example\n\nBody here.\n",
    )
    path = store.save(skill, "L2_domain/example.skill.md")
    assert path.exists()
    loaded = store.load_path(path)
    assert isinstance(loaded, Skill)
    assert loaded.name == "example"
    assert loaded.product == "forge"
    assert loaded.confidence == 0.5
    assert loaded.composes_with == ["master"]
    assert loaded.applies_to.files == ["**/*.py"]
    assert loaded.body.strip() == "# Example\n\nBody here.".strip()
    assert loaded.provenance.revision_count == 1
