"""Product deletion helper used by CLI/admin cleanup flows."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from nexus.config import NexusConfig
from nexus.council.queue import ProposalQueue
from nexus.graph.store import create_graph_store
from nexus.ingest.indexer_factory import create_indexer
from nexus.registry import Registry
from nexus.retrieval.repomap import repomap_path_for
from nexus.skills.store import SkillStore


@dataclass
class DeleteProductReport:
    product_id: str
    registry: dict[str, int] = field(default_factory=dict)
    queue: dict[str, int | list[str]] = field(default_factory=dict)
    skills: int = 0
    index: dict[str, int] = field(default_factory=dict)
    graph_deleted: bool = False
    repomap_deleted: bool = False
    checkpoints: int = 0


async def delete_product(
    *,
    product_id: str,
    config: NexusConfig,
    dry_run: bool,
    skip_qdrant: bool = False,
) -> DeleteProductReport:
    registry = Registry(config.storage.proposal_queue.parent / "registry.db")
    queue = ProposalQueue(config.storage.proposal_queue)
    store = SkillStore(config.hierarchy_root)

    report = DeleteProductReport(product_id=product_id)
    report.queue = _queue_counts(queue, product_id)
    report.registry = _registry_counts(registry, product_id)
    report.skills = sum(1 for skill in store.iter_skills() if skill.product == product_id)

    repomap = repomap_path_for(config.storage.proposal_queue.parent, product_id)
    report.repomap_deleted = repomap.exists()
    session_ids = list(report.queue.get("session_ids", []))
    report.checkpoints = _checkpoint_count(config.storage.council_checkpoint, session_ids)

    if not skip_qdrant:
        indexer = create_indexer(config)
        try:
            report.index = {
                "code": await indexer.count(product_id=product_id, vector_kind="code"),
                "text": await indexer.count(product_id=product_id, vector_kind="text"),
            }
            if not dry_run:
                report.index = await indexer.delete_by_product(product_id=product_id)
        finally:
            await indexer.aclose()

    if dry_run:
        return report

    graph_store = create_graph_store(config)
    try:
        await graph_store.delete_product(product_id=product_id)
        report.graph_deleted = True
    finally:
        await graph_store.aclose()

    registry.delete_product(product_id)
    queue.delete_product(product_id)
    store.delete_product(product_id)
    if repomap.exists():
        repomap.unlink()
    _delete_checkpoints(config.storage.council_checkpoint, session_ids)
    return report


def _registry_counts(registry: Registry, product_id: str) -> dict[str, int]:
    with registry._conn() as conn:
        return {
            "products": conn.execute(
                "SELECT COUNT(*) FROM products WHERE id = ?", (product_id,)
            ).fetchone()[0],
            "sources": conn.execute(
                "SELECT COUNT(*) FROM sources WHERE product_id = ?", (product_id,)
            ).fetchone()[0],
            "source_resources": conn.execute(
                "SELECT COUNT(*) FROM source_resources WHERE product_id = ?",
                (product_id,),
            ).fetchone()[0],
            "source_sync_runs": conn.execute(
                "SELECT COUNT(*) FROM source_sync_runs WHERE product_id = ?",
                (product_id,),
            ).fetchone()[0],
        }


def _queue_counts(queue: ProposalQueue, product_id: str) -> dict[str, int | list[str]]:
    with queue._conn() as conn:
        session_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM sessions WHERE product_id = ?", (product_id,)
            ).fetchall()
        ]
        return {
            "proposals": conn.execute(
                "SELECT COUNT(*) FROM proposals WHERE product_id = ?", (product_id,)
            ).fetchone()[0],
            "sessions": len(session_ids),
            "session_ids": session_ids,
        }


def _checkpoint_count(path: Path, session_ids: list[str]) -> int:
    if not path.exists() or not session_ids:
        return 0
    try:
        with sqlite3.connect(path) as conn:
            return sum(
                _count_checkpoint_table(conn, table, session_ids)
                for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs")
            )
    except sqlite3.Error:
        return 0


def _delete_checkpoints(path: Path, session_ids: list[str]) -> int:
    if not path.exists() or not session_ids:
        return 0
    deleted = 0
    try:
        with sqlite3.connect(path) as conn:
            for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
                deleted += _delete_checkpoint_table(conn, table, session_ids)
    except sqlite3.Error:
        return deleted
    return deleted


def _count_checkpoint_table(
    conn: sqlite3.Connection, table: str, session_ids: list[str]
) -> int:
    if not _has_thread_id(conn, table):
        return 0
    placeholders = ",".join("?" for _ in session_ids)
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE thread_id IN ({placeholders})",
        session_ids,
    ).fetchone()[0]


def _delete_checkpoint_table(
    conn: sqlite3.Connection, table: str, session_ids: list[str]
) -> int:
    if not _has_thread_id(conn, table):
        return 0
    placeholders = ",".join("?" for _ in session_ids)
    cur = conn.execute(
        f"DELETE FROM {table} WHERE thread_id IN ({placeholders})",
        session_ids,
    )
    return cur.rowcount


def _has_thread_id(conn: sqlite3.Connection, table: str) -> bool:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchall()
    if not rows:
        return False
    return any(row[1] == "thread_id" for row in conn.execute(f"PRAGMA table_info({table})"))
