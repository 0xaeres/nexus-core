from nexus.tasks.pr_review import _product_for_repo, _render_user, _wrap_comment


def test_product_from_repo_topic() -> None:
    payload = {"repository": {"topics": ["python", "nexus-product:atlas"]}}
    assert _product_for_repo(payload, default="forge") == "atlas"


def test_product_default_when_no_topic() -> None:
    payload = {"repository": {"topics": ["python"]}}
    assert _product_for_repo(payload, default="forge") == "forge"


def test_wrap_comment_includes_skill_chips() -> None:
    skills = [
        {"name": "pda-seed-validation"},
        {"name": "owasp-input-validation"},
    ]
    out = _wrap_comment("body text", skills)
    assert "[skill: pda-seed-validation]" in out
    assert "[skill: owasp-input-validation]" in out
    assert "body text" in out


def test_render_user_handles_empty_skills() -> None:
    out = _render_user("diff", [])
    assert "no curated skills matched" in out
    assert "diff" in out
