from pathlib import Path

from nexus.council.queue import OrgProposalQueue


def test_enqueue_then_list_org_proposal(tmp_path: Path) -> None:
    q = OrgProposalQueue(tmp_path / "q.db")
    q.enqueue_org_proposal(
        proposal_id="orgp_1",
        name="ts-conventions",
        kind="language",
        body="# Body",
        quality_score=0.8,
        external_sources=["https://example.com/a"],
        applies_to={"files": ["**/*.ts"], "contexts": ["code-review"]},
    )
    rows = q.list_org_proposals(status="pending")
    assert len(rows) == 1
    assert rows[0]["name"] == "ts-conventions"
    assert rows[0]["external_sources"] == ["https://example.com/a"]
    assert rows[0]["applies_to"]["files"] == ["**/*.ts"]


def test_ratify_transitions_status(tmp_path: Path) -> None:
    q = OrgProposalQueue(tmp_path / "q.db")
    q.enqueue_org_proposal(
        proposal_id="orgp_a",
        name="x",
        kind="security",
        body="",
        quality_score=0.5,
        external_sources=[],
        applies_to={},
    )
    assert q.ratify_org_proposal("orgp_a", actor="admin@org")
    row = q.get_org_proposal("orgp_a")
    assert row is not None
    assert row["status"] == "ratified"
    assert row["ratified_by"] == "admin@org"


def test_change_request_lifecycle(tmp_path: Path) -> None:
    q = OrgProposalQueue(tmp_path / "q.db")
    q.file_change_request(
        request_id="cr_1",
        org_skill_id="org/owasp-input-validation",
        skill_kind="security",
        title="Relax len check",
        proposed_diff="--- a\n+++ b\n@@ ...",
        rationale="legacy endpoint",
        requested_by="sme@org",
    )
    assert q.attach_agent_verdict(
        "cr_1",
        agent_verdict={
            "agent": "security_sentinel",
            "verdict": "medium_risk",
            "analysis": "...",
            "recommendation": "...",
        },
    )
    row = q.get_change_request("cr_1")
    assert row is not None
    assert row["status"] == "awaiting_approver"
    assert row["agent_analysis"]["verdict"] == "medium_risk"

    assert q.decide_change_request("cr_1", outcome="rejected", actor="admin", reason="no")
    row = q.get_change_request("cr_1")
    assert row is not None
    assert row["status"] == "rejected"
    assert row["rejection_reason"] == "no"
