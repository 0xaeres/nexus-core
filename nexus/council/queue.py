"""SQLite-backed pending proposal queue.

One row per `SkillProposal`. Status transitions: pending → approved | rejected | edited.
This is the source of truth that `/proposals` endpoints read from.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from nexus.skills.models import SkillProposal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id            TEXT PRIMARY KEY,
    session_id    TEXT,
    product_id    TEXT NOT NULL,
    skill_kind    TEXT NOT NULL,
    name          TEXT NOT NULL,
    body          TEXT NOT NULL,
    citations_js  TEXT NOT NULL,
    confidence    REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    critique_js   TEXT,
    created_at    TEXT NOT NULL,
    approved_by   TEXT,
    approved_at   TEXT,
    deliberation_js TEXT,
    costs_js      TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_status
    ON proposals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_proposals_product
    ON proposals(product_id, status);

-- Lightweight session table: one row per council run, for the SSE endpoint.
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    product_id    TEXT NOT NULL,
    skill_kind    TEXT NOT NULL,
    topic         TEXT NOT NULL,
    proposal_id   TEXT,
    status        TEXT NOT NULL DEFAULT 'completed',
    deliberation_js TEXT NOT NULL DEFAULT '[]',
    costs_js      TEXT NOT NULL DEFAULT '[]',
    started_at    TEXT NOT NULL,
    completed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_product
    ON sessions(product_id, started_at DESC);

-- Org Library proposals (Curator output).
CREATE TABLE IF NOT EXISTS org_proposals (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,                  -- tech_stack | language | security
    body        TEXT NOT NULL,
    quality_score REAL NOT NULL DEFAULT 0.0,
    external_sources_js TEXT NOT NULL DEFAULT '[]',
    applies_to_js       TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | ratified | rejected
    created_at  TEXT NOT NULL,
    ratified_by TEXT,
    ratified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_org_proposals_status
    ON org_proposals(status, created_at DESC);

-- Change requests against published Org skills.
CREATE TABLE IF NOT EXISTS change_requests (
    id            TEXT PRIMARY KEY,
    org_skill_id  TEXT NOT NULL,
    skill_kind    TEXT NOT NULL,
    title         TEXT NOT NULL,
    proposed_diff TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    requested_by  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'filed',   -- filed | awaiting_approver |
                                                   -- approved | rejected
    agent_js      TEXT,                            -- AgentVerdict json
    created_at    TEXT NOT NULL,
    decided_by    TEXT,
    decided_at    TEXT,
    rejection_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_change_requests_skill
    ON change_requests(org_skill_id, status);
"""


class ProposalQueue:
    """Thin synchronous wrapper. SQLite is fast enough that we don't need async."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------ write

    def enqueue(
        self,
        proposal: SkillProposal,
        *,
        session_id: str,
        product_id: str,
        skill_kind: str,
        deliberation: list[dict] | None = None,
        costs: list[dict] | None = None,
    ) -> None:
        critique_js = (
            proposal.adversary_critique.model_dump_json()
            if proposal.adversary_critique
            else None
        )
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO proposals
                   (id, session_id, product_id, skill_kind, name, body, citations_js,
                    confidence, status, critique_js, created_at, approved_by, approved_at,
                    deliberation_js, costs_js)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    proposal.id,
                    session_id,
                    product_id,
                    skill_kind,
                    proposal.name,
                    proposal.body,
                    json.dumps([c.model_dump() for c in proposal.citations]),
                    proposal.confidence,
                    proposal.status,
                    critique_js,
                    proposal.created_at,
                    proposal.approved_by,
                    proposal.approved_at,
                    json.dumps(deliberation or []),
                    json.dumps(costs or []),
                ),
            )

    def record_session(
        self,
        *,
        session_id: str,
        product_id: str,
        skill_kind: str,
        topic: str,
        proposal_id: str | None,
        deliberation: list[dict],
        costs: list[dict],
        started_at: str,
        completed_at: str,
        status: str = "completed",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (id, product_id, skill_kind, topic, proposal_id, status,
                    deliberation_js, costs_js, started_at, completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id,
                    product_id,
                    skill_kind,
                    topic,
                    proposal_id,
                    status,
                    json.dumps(deliberation),
                    json.dumps(costs),
                    started_at,
                    completed_at,
                ),
            )

    def get_session(self, session_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["deliberation"] = json.loads(d.pop("deliberation_js") or "[]")
        d["costs"] = json.loads(d.pop("costs_js") or "[]")
        return d

    def list_sessions(self, *, product_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, product_id, skill_kind, topic, proposal_id, status, "
                "started_at, completed_at FROM sessions "
                "WHERE product_id = ? ORDER BY started_at DESC",
                (product_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_status(
        self,
        proposal_id: str,
        *,
        status: str,
        actor: str | None = None,
        body: str | None = None,
    ) -> bool:
        ts = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE proposals
                   SET status = ?,
                       approved_by = COALESCE(?, approved_by),
                       approved_at = CASE WHEN ? IN ('approved','rejected','edited')
                                          THEN ? ELSE approved_at END,
                       body = COALESCE(?, body)
                   WHERE id = ?""",
                (status, actor, status, ts, body, proposal_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------ read

    def get(self, proposal_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list(self, *, status: str | None = None, product_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM proposals WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if product_id:
            sql += " AND product_id = ?"
            args.append(product_id)
        sql += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["citations"] = json.loads(d.pop("citations_js") or "[]")
    crit = d.pop("critique_js", None)
    d["adversary_critique"] = json.loads(crit) if crit else None
    delib = d.pop("deliberation_js", None)
    d["deliberation"] = json.loads(delib) if delib else []
    costs = d.pop("costs_js", None)
    d["costs"] = json.loads(costs) if costs else []
    return d


# ---------------------------------------------------------------- org library


class OrgProposalQueue:
    """Org Library proposals + change requests, sharing the proposal queue DB."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        try:
            yield conn
        finally:
            conn.close()

    # ----------------------------------------------------- proposals

    def enqueue_org_proposal(
        self,
        *,
        proposal_id: str,
        name: str,
        kind: str,
        body: str,
        quality_score: float,
        external_sources: list[str],
        applies_to: dict,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO org_proposals
                   (id, name, kind, body, quality_score, external_sources_js,
                    applies_to_js, status, created_at)
                   VALUES (?,?,?,?,?,?,?,'pending',?)""",
                (
                    proposal_id,
                    name,
                    kind,
                    body,
                    quality_score,
                    json.dumps(external_sources),
                    json.dumps(applies_to),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def list_org_proposals(self, *, status: str | None = "pending") -> list[dict]:
        sql = "SELECT * FROM org_proposals"
        args: list = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_org_proposal(r) for r in rows]

    def get_org_proposal(self, proposal_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM org_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return _row_to_org_proposal(row) if row else None

    def ratify_org_proposal(self, proposal_id: str, *, actor: str) -> bool:
        ts = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE org_proposals SET status='ratified', ratified_by=?, "
                "ratified_at=? WHERE id=?",
                (actor, ts, proposal_id),
            )
            return cur.rowcount > 0

    # ----------------------------------------------------- change requests

    def file_change_request(
        self,
        *,
        request_id: str,
        org_skill_id: str,
        skill_kind: str,
        title: str,
        proposed_diff: str,
        rationale: str,
        requested_by: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO change_requests
                   (id, org_skill_id, skill_kind, title, proposed_diff,
                    rationale, requested_by, status, created_at)
                   VALUES (?,?,?,?,?,?,?,'filed',?)""",
                (
                    request_id,
                    org_skill_id,
                    skill_kind,
                    title,
                    proposed_diff,
                    rationale,
                    requested_by,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def attach_agent_verdict(
        self, request_id: str, *, agent_verdict: dict
    ) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE change_requests SET status='awaiting_approver', "
                "agent_js=? WHERE id=?",
                (json.dumps(agent_verdict), request_id),
            )
            return cur.rowcount > 0

    def decide_change_request(
        self,
        request_id: str,
        *,
        outcome: str,
        actor: str,
        reason: str | None = None,
    ) -> bool:
        if outcome not in ("approved", "rejected"):
            raise ValueError(f"unknown outcome: {outcome}")
        ts = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE change_requests SET status=?, decided_by=?, decided_at=?, "
                "rejection_reason=? WHERE id=?",
                (outcome, actor, ts, reason, request_id),
            )
            return cur.rowcount > 0

    def list_change_requests(
        self, *, org_skill_id: str | None = None, status: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM change_requests WHERE 1=1"
        args: list = []
        if org_skill_id:
            sql += " AND org_skill_id = ?"
            args.append(org_skill_id)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_change_request(r) for r in rows]

    def get_change_request(self, request_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM change_requests WHERE id = ?", (request_id,)
            ).fetchone()
        return _row_to_change_request(row) if row else None


def _row_to_org_proposal(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["external_sources"] = json.loads(d.pop("external_sources_js") or "[]")
    d["applies_to"] = json.loads(d.pop("applies_to_js") or "{}")
    return d


def _row_to_change_request(row: sqlite3.Row) -> dict:
    d = dict(row)
    agent = d.pop("agent_js", None)
    d["agent_analysis"] = json.loads(agent) if agent else None
    return d
