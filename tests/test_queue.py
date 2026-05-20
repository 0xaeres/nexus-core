from pathlib import Path

from nexus.council.queue import ProposalQueue
from nexus.skills.models import Citation, SkillProposal


def _make_proposal(name: str = "demo", confidence: float = 0.5) -> SkillProposal:
    return SkillProposal(
        id=f"prop_{name}",
        name=name,
        body="# Demo\n\nBody [file: a/b.py:10].",
        citations=[Citation(file="a/b.py", line=10, excerpt="x")],
        confidence=confidence,
        status="pending",
        created_at="2026-05-18T00:00:00Z",
    )


def test_enqueue_then_list_returns_proposal(tmp_path: Path) -> None:
    queue = ProposalQueue(tmp_path / "proposals.db")
    queue.enqueue(
        _make_proposal(),
        session_id="cs_x",
        product_id="forge",
        skill_kind="product_domain",
    )
    pending = queue.list(status="pending")
    assert len(pending) == 1
    assert pending[0]["name"] == "demo"
    assert pending[0]["citations"] == [
        {"id": None, "file": "a/b.py", "line": 10, "excerpt": "x"}
    ]


def test_list_filters_by_product(tmp_path: Path) -> None:
    queue = ProposalQueue(tmp_path / "p.db")
    queue.enqueue(
        _make_proposal("a"), session_id="s1", product_id="forge", skill_kind="master"
    )
    queue.enqueue(
        _make_proposal("b"), session_id="s2", product_id="atlas", skill_kind="master"
    )
    assert {p["name"] for p in queue.list(product_id="forge")} == {"a"}
    assert {p["name"] for p in queue.list(product_id="atlas")} == {"b"}


def test_update_status_transitions(tmp_path: Path) -> None:
    queue = ProposalQueue(tmp_path / "u.db")
    p = _make_proposal()
    queue.enqueue(p, session_id="s", product_id="forge", skill_kind="master")
    assert queue.update_status(p.id, status="rejected", actor="reviewer@x")
    row = queue.get(p.id)
    assert row is not None
    assert row["status"] == "rejected"
    assert row["approved_by"] == "reviewer@x"
    assert row["approved_at"]


def test_record_and_get_session(tmp_path: Path) -> None:
    queue = ProposalQueue(tmp_path / "s.db")
    queue.record_session(
        session_id="cs_demo",
        product_id="forge",
        skill_kind="master",
        topic="overview",
        proposal_id="prop_demo",
        deliberation=[{"agent": "archaeologist", "body": "found stuff"}],
        costs=[{"agent": "archaeologist", "prompt_tokens": 100, "completion_tokens": 50}],
        started_at="2026-05-18T00:00:00Z",
        completed_at="2026-05-18T00:00:42Z",
    )
    s = queue.get_session("cs_demo")
    assert s is not None
    assert s["topic"] == "overview"
    assert s["deliberation"] == [{"agent": "archaeologist", "body": "found stuff"}]
    assert s["costs"][0]["prompt_tokens"] == 100


def test_list_sessions_orders_newest_first(tmp_path: Path) -> None:
    queue = ProposalQueue(tmp_path / "ls.db")
    for i, ts in enumerate(["2026-05-18T00:00:00Z", "2026-05-18T01:00:00Z"]):
        queue.record_session(
            session_id=f"cs_{i}",
            product_id="forge",
            skill_kind="master",
            topic=f"topic_{i}",
            proposal_id=None,
            deliberation=[],
            costs=[],
            started_at=ts,
            completed_at=ts,
        )
    sessions = queue.list_sessions(product_id="forge")
    assert [s["id"] for s in sessions] == ["cs_1", "cs_0"]
