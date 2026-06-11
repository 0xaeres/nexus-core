"""SQLite-backed pending proposal queue.

One row per `SkillProposal`. Status transitions: pending → approved | rejected | edited.
This is the source of truth that the `/proposals` endpoints read from.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
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
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    tier          TEXT NOT NULL DEFAULT 'domain',
    parent        TEXT,
    related_js    TEXT NOT NULL DEFAULT '[]',
    coverage_js   TEXT NOT NULL DEFAULT '{}',
    body          TEXT NOT NULL,
    citations_js  TEXT NOT NULL,
    confidence    REAL NOT NULL,
    eval_status   TEXT NOT NULL DEFAULT 'not_run',
    eval_summary  TEXT NOT NULL DEFAULT '',
    eval_failures_js TEXT NOT NULL DEFAULT '[]',
    quality_score REAL NOT NULL DEFAULT 0,
    signals_used_js TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'pending',
    critique_js   TEXT,
    created_at    TEXT NOT NULL,
    approved_by   TEXT,
    approved_at   TEXT,
    skill_path    TEXT,
    git_committed INTEGER NOT NULL DEFAULT 0,
    skill_index_status TEXT NOT NULL DEFAULT 'not_started',
    skill_index_error TEXT NOT NULL DEFAULT '',
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
    topic         TEXT NOT NULL,
    proposal_id   TEXT,
    proposal_ids_js TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'completed',
    deliberation_js TEXT NOT NULL DEFAULT '[]',
    costs_js      TEXT NOT NULL DEFAULT '[]',
    started_at    TEXT NOT NULL,
    completed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_product
    ON sessions(product_id, started_at DESC);

CREATE TABLE IF NOT EXISTS skill_signals (
    id            TEXT PRIMARY KEY,
    product_id    TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    skill_name    TEXT,
    proposal_id   TEXT,
    session_id    TEXT,
    text          TEXT NOT NULL,
    metadata_js   TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skill_signals_product
    ON skill_signals(product_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_signals_skill
    ON skill_signals(product_id, skill_name, created_at DESC);

CREATE TABLE IF NOT EXISTS skill_eval_runs (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    product_id    TEXT NOT NULL,
    suite_version TEXT NOT NULL,
    status        TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skill_eval_runs_session
    ON skill_eval_runs(session_id);

CREATE TABLE IF NOT EXISTS skill_eval_results (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    product_id    TEXT NOT NULL,
    proposal_id   TEXT,
    skill_name    TEXT NOT NULL,
    status        TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    failures_js   TEXT NOT NULL DEFAULT '[]',
    quality_score REAL NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    signals_used_js TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skill_eval_results_product
    ON skill_eval_results(product_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_eval_results_session
    ON skill_eval_results(session_id);
"""


class ProposalQueue:
    """Thin synchronous wrapper. SQLite is fast enough that we don't need async."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            _ensure_columns(conn)

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
                   (id, session_id, product_id, name, description, tier, parent, related_js,
                    coverage_js, body, citations_js, confidence, eval_status, eval_summary,
                    eval_failures_js, quality_score, signals_used_js, status, critique_js,
                    created_at, approved_by, approved_at, deliberation_js, costs_js)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    proposal.id,
                    session_id,
                    product_id,
                    proposal.name,
                    proposal.description,
                    proposal.tier,
                    proposal.parent,
                    json.dumps(proposal.related),
                    proposal.coverage.model_dump_json(),
                    proposal.body,
                    json.dumps([c.model_dump() for c in proposal.citations]),
                    proposal.confidence,
                    proposal.eval_status,
                    proposal.eval_summary,
                    json.dumps(proposal.eval_failures),
                    proposal.quality_score,
                    json.dumps(proposal.signals_used),
                    proposal.status,
                    critique_js,
                    proposal.created_at,
                    proposal.approved_by,
                    proposal.approved_at,
                    json.dumps(deliberation or []),
                    json.dumps(costs or []),
                ),
            )

    def record_skill_signal(
        self,
        *,
        product_id: str,
        source_type: str,
        text: str,
        skill_name: str | None = None,
        proposal_id: str | None = None,
        session_id: str | None = None,
        metadata: dict | None = None,
        created_at: str | None = None,
    ) -> str:
        signal_id = str(uuid.uuid4())
        ts = created_at or datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO skill_signals
                   (id, product_id, source_type, skill_name, proposal_id, session_id,
                    text, metadata_js, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    signal_id,
                    product_id,
                    source_type,
                    skill_name,
                    proposal_id,
                    session_id,
                    text,
                    json.dumps(metadata or {}),
                    ts,
                ),
            )
        return signal_id

    def list_skill_signals(
        self,
        *,
        product_id: str,
        skill_name: str | None = None,
        source_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        sql = "SELECT * FROM skill_signals WHERE product_id = ?"
        args: list = [product_id]
        if skill_name:
            sql += " AND skill_name = ?"
            args.append(skill_name)
        if source_type:
            sql += " AND source_type = ?"
            args.append(source_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_signal_row_to_dict(r) for r in rows]

    def record_eval_run(
        self,
        *,
        run_id: str,
        session_id: str,
        product_id: str,
        suite_version: str,
        status: str,
        summary: str = "",
        created_at: str | None = None,
    ) -> None:
        ts = created_at or datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO skill_eval_runs
                   (id, session_id, product_id, suite_version, status, summary, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_id, session_id, product_id, suite_version, status, summary, ts),
            )

    def record_eval_result(
        self,
        *,
        run_id: str,
        session_id: str,
        product_id: str,
        skill_name: str,
        status: str,
        summary: str = "",
        failures: list[str] | None = None,
        quality_score: float = 0.0,
        attempts: int = 0,
        signals_used: list[str] | None = None,
        proposal_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        result_id = str(uuid.uuid4())
        ts = created_at or datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO skill_eval_results
                   (id, run_id, session_id, product_id, proposal_id, skill_name, status,
                    summary, failures_js, quality_score, attempts, signals_used_js, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result_id,
                    run_id,
                    session_id,
                    product_id,
                    proposal_id,
                    skill_name,
                    status,
                    summary,
                    json.dumps(failures or []),
                    quality_score,
                    attempts,
                    json.dumps(signals_used or []),
                    ts,
                ),
            )
        return result_id

    def record_session(
        self,
        *,
        session_id: str,
        product_id: str,
        topic: str,
        proposal_id: str | None,
        deliberation: list[dict],
        costs: list[dict],
        started_at: str,
        completed_at: str,
        proposal_ids: list[str] | None = None,
        status: str = "completed",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (id, product_id, topic, proposal_id, proposal_ids_js, status,
                    deliberation_js, costs_js, started_at, completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id,
                    product_id,
                    topic,
                    proposal_id,
                    json.dumps(proposal_ids or ([proposal_id] if proposal_id else [])),
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
        d["proposal_ids"] = json.loads(d.pop("proposal_ids_js", None) or "[]")
        d["eval_results"] = self.list_eval_results(session_id=session_id)
        return d

    def list_sessions(self, *, product_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, product_id, topic, proposal_id, proposal_ids_js, status, "
                "started_at, completed_at FROM sessions "
                "WHERE product_id = ? ORDER BY started_at DESC",
                (product_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["proposal_ids"] = json.loads(d.pop("proposal_ids_js", None) or "[]")
            out.append(d)
        return out

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

    def record_publish_result(
        self,
        proposal_id: str,
        *,
        skill_path: str,
        git_committed: bool,
        skill_index_status: str,
        skill_index_error: str = "",
    ) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE proposals
                   SET skill_path = ?,
                       git_committed = ?,
                       skill_index_status = ?,
                       skill_index_error = ?
                   WHERE id = ?""",
                (
                    skill_path,
                    1 if git_committed else 0,
                    skill_index_status,
                    skill_index_error,
                    proposal_id,
                ),
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

    def list_eval_results(
        self,
        *,
        product_id: str | None = None,
        session_id: str | None = None,
        skill_name: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        sql = "SELECT * FROM skill_eval_results WHERE 1=1"
        args: list = []
        if product_id:
            sql += " AND product_id = ?"
            args.append(product_id)
        if session_id:
            sql += " AND session_id = ?"
            args.append(session_id)
        if skill_name:
            sql += " AND skill_name = ?"
            args.append(skill_name)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_eval_result_row_to_dict(r) for r in rows]

    def delete_product(self, product_id: str) -> dict[str, int | list[str]]:
        with self._conn() as conn:
            session_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM sessions WHERE product_id = ?", (product_id,)
                ).fetchall()
            ]
            proposal_count = conn.execute(
                "SELECT COUNT(*) FROM proposals WHERE product_id = ?", (product_id,)
            ).fetchone()[0]
            session_count = len(session_ids)
            conn.execute("DELETE FROM proposals WHERE product_id = ?", (product_id,))
            conn.execute("DELETE FROM sessions WHERE product_id = ?", (product_id,))
            conn.execute("DELETE FROM skill_signals WHERE product_id = ?", (product_id,))
            conn.execute("DELETE FROM skill_eval_runs WHERE product_id = ?", (product_id,))
            conn.execute("DELETE FROM skill_eval_results WHERE product_id = ?", (product_id,))
        return {
            "proposals": proposal_count,
            "sessions": session_count,
            "session_ids": session_ids,
        }


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["citations"] = json.loads(d.pop("citations_js") or "[]")
    d["related"] = json.loads(d.pop("related_js", None) or "[]")
    d["coverage"] = json.loads(d.pop("coverage_js", None) or "{}")
    d["eval_failures"] = json.loads(d.pop("eval_failures_js", None) or "[]")
    d["signals_used"] = json.loads(d.pop("signals_used_js", None) or "[]")
    crit = d.pop("critique_js", None)
    d["adversary_critique"] = json.loads(crit) if crit else None
    delib = d.pop("deliberation_js", None)
    d["deliberation"] = json.loads(delib) if delib else []
    costs = d.pop("costs_js", None)
    d["costs"] = json.loads(costs) if costs else []
    return d


def _signal_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["metadata"] = json.loads(d.pop("metadata_js", None) or "{}")
    return d


def _eval_result_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["failures"] = json.loads(d.pop("failures_js", None) or "[]")
    d["signals_used"] = json.loads(d.pop("signals_used_js", None) or "[]")
    return d


def _ensure_columns(conn: sqlite3.Connection) -> None:
    proposal_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()
    }
    for name, ddl in {
        "description": "ALTER TABLE proposals ADD COLUMN description TEXT NOT NULL DEFAULT ''",
        "tier": "ALTER TABLE proposals ADD COLUMN tier TEXT NOT NULL DEFAULT 'domain'",
        "parent": "ALTER TABLE proposals ADD COLUMN parent TEXT",
        "related_js": "ALTER TABLE proposals ADD COLUMN related_js TEXT NOT NULL DEFAULT '[]'",
        "coverage_js": "ALTER TABLE proposals ADD COLUMN coverage_js TEXT NOT NULL DEFAULT '{}'",
        "eval_status": "ALTER TABLE proposals ADD COLUMN eval_status TEXT NOT NULL DEFAULT 'not_run'",
        "eval_summary": "ALTER TABLE proposals ADD COLUMN eval_summary TEXT NOT NULL DEFAULT ''",
        "eval_failures_js": "ALTER TABLE proposals ADD COLUMN eval_failures_js TEXT NOT NULL DEFAULT '[]'",
        "quality_score": "ALTER TABLE proposals ADD COLUMN quality_score REAL NOT NULL DEFAULT 0",
        "signals_used_js": "ALTER TABLE proposals ADD COLUMN signals_used_js TEXT NOT NULL DEFAULT '[]'",
        "skill_path": "ALTER TABLE proposals ADD COLUMN skill_path TEXT",
        "git_committed": "ALTER TABLE proposals ADD COLUMN git_committed INTEGER NOT NULL DEFAULT 0",
        "skill_index_status": (
            "ALTER TABLE proposals ADD COLUMN "
            "skill_index_status TEXT NOT NULL DEFAULT 'not_started'"
        ),
        "skill_index_error": (
            "ALTER TABLE proposals ADD COLUMN "
            "skill_index_error TEXT NOT NULL DEFAULT ''"
        ),
    }.items():
        if name not in proposal_cols:
            conn.execute(ddl)

    session_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if "proposal_ids_js" not in session_cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN proposal_ids_js TEXT NOT NULL DEFAULT '[]'"
        )
