"""SQLite registry for products, users, runtime sources, and setup state."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from nexus.auth.token_cipher import TokenCipher, TokenCipherError

log = logging.getLogger(__name__)

# Keys whose values are encrypted at rest in source config blobs.
_SECRET_KEY_HINTS = ("token", "api_key", "password", "secret")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _get_cipher() -> TokenCipher | None:
    key = os.environ.get("NEXUS_TOKEN_KEY")
    if not key:
        return None
    try:
        return TokenCipher(key)
    except TokenCipherError:
        log.exception("NEXUS_TOKEN_KEY is set but invalid")
        return None


def _is_secret_key(key: str) -> bool:
    return any(s in key.lower() for s in _SECRET_KEY_HINTS)


def _encrypt_config(config: dict, cipher: TokenCipher | None) -> dict:
    if not cipher and any(_is_secret_key(k) and isinstance(v, str) and v for k, v in config.items()):
        raise TokenCipherError(
            "NEXUS_TOKEN_KEY is required to store connector secrets; generate one "
            "with TokenCipher.generate_key()"
        )
    out: dict = {}
    for k, v in config.items():
        if cipher and isinstance(v, str) and v and _is_secret_key(k):
            out[k] = "enc:" + cipher.encrypt(v)
        else:
            out[k] = v
    return out


def _decrypt_config(config: dict, cipher: TokenCipher | None) -> dict:
    out: dict = {}
    for k, v in config.items():
        if isinstance(v, str) and v.startswith("enc:"):
            if cipher:
                try:
                    out[k] = cipher.decrypt(v[4:])
                except TokenCipherError:
                    log.error("Failed to decrypt config key %r — returning redacted value", k)
                    out[k] = ""
            else:
                # Key was encrypted but NEXUS_TOKEN_KEY is now missing.
                log.warning(
                    "Encrypted value for %r but NEXUS_TOKEN_KEY not set; returning empty", k
                )
                out[k] = ""
        else:
            out[k] = v
    return out


_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    tagline         TEXT NOT NULL DEFAULT '',
    owner_js        TEXT NOT NULL DEFAULT '{}',
    onboarded_at    TEXT NOT NULL,
    master_skill_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    products_js TEXT NOT NULL DEFAULT '[]'
);

-- Runtime-added sources. Merged with nexus.yaml connectors at read time.
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'connected',
    config_js   TEXT NOT NULL DEFAULT '{}',
    last_sync   TEXT,
    resource_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_product
    ON sources(product_id);

CREATE TABLE IF NOT EXISTS product_members (
    product_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (product_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_product_members_user
    ON product_members(user_id, product_id);

CREATE TABLE IF NOT EXISTS source_resources (
    product_id        TEXT NOT NULL,
    source_key        TEXT NOT NULL,
    resource_uri      TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    mime              TEXT NOT NULL DEFAULT '',
    size_bytes        INTEGER,
    last_seen_sync    TEXT NOT NULL,
    chunk_ids_js      TEXT NOT NULL DEFAULT '[]',
    indexed_at        TEXT NOT NULL,
    embedding_version TEXT NOT NULL DEFAULT '',
    enrichment_version TEXT NOT NULL DEFAULT '',
    enrichment_status TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (product_id, source_key, resource_uri)
);

CREATE INDEX IF NOT EXISTS idx_source_resources_source
    ON source_resources(product_id, source_key);

CREATE TABLE IF NOT EXISTS source_sync_runs (
    id          TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL,
    source_key  TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    added       INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    unchanged   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running'
);

CREATE INDEX IF NOT EXISTS idx_source_sync_runs_source
    ON source_sync_runs(product_id, source_key, started_at DESC);

CREATE TABLE IF NOT EXISTS enrichment_jobs (
    id              TEXT PRIMARY KEY,
    product_id      TEXT NOT NULL,
    source_key      TEXT NOT NULL,
    resource_uri    TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    mime            TEXT NOT NULL DEFAULT '',
    size_bytes      INTEGER,
    last_modified   TEXT,
    content_hash    TEXT NOT NULL,
    content         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_enrichment_jobs_status
    ON enrichment_jobs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_enrichment_jobs_product
    ON enrichment_jobs(product_id, status);
"""


class Registry:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            _ensure_registry_columns(conn)
            _backfill_product_members(conn)
        self._seed_defaults()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _seed_defaults(self) -> None:
        with self._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if n == 0:
                conn.execute(
                    """INSERT INTO users (id, name, role, products_js)
                       VALUES (?,?,?,?)""",
                    ("admin", "Admin", "org_admin", json.dumps([])),
                )

    # ------------------------------------------------------------ products

    def list_products(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
        return [_row_to_product(r) for r in rows]

    def get_product(self, product_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return _row_to_product(row) if row else None

    def upsert_product(self, product: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO products
                   (id, name, tagline, owner_js, onboarded_at)
                   VALUES (?,?,?,?,?)""",
                (
                    product["id"],
                    product["name"],
                    product.get("tagline", ""),
                    json.dumps(product.get("owner", {})),
                    product["onboardedAt"],
                ),
            )

    # ------------------------------------------------------------ users

    def get_user(self, user_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["products"] = json.loads(d.pop("products_js"))
        return d

    def list_users(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY name").fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            d["products"] = json.loads(d.pop("products_js"))
            out.append(d)
        return out


def _ensure_registry_columns(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(source_resources)").fetchall()
    }
    if "enrichment_version" not in existing:
        conn.execute(
            "ALTER TABLE source_resources "
            "ADD COLUMN enrichment_version TEXT NOT NULL DEFAULT ''"
        )
    if "enrichment_status" not in existing:
        conn.execute(
            "ALTER TABLE source_resources "
            "ADD COLUMN enrichment_status TEXT NOT NULL DEFAULT ''"
        )


def _backfill_product_members(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, products_js FROM users").fetchall()
    now = _now_iso()
    for row in rows:
        try:
            products = json.loads(row["products_js"] or "[]")
        except json.JSONDecodeError:
            continue
        for entry in products:
            product_id = entry.get("id") if isinstance(entry, dict) else entry
            role = entry.get("role", "owner") if isinstance(entry, dict) else "owner"
            if not product_id:
                continue
            if role not in {"owner", "editor", "viewer"}:
                role = "owner"
            conn.execute(
                """INSERT OR IGNORE INTO product_members
                   (product_id, user_id, role, created_at)
                   VALUES (?,?,?,?)""",
                (str(product_id), row["id"], role, now),
            )


def _row_to_product(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["owner"] = json.loads(d.pop("owner_js"))
    d["onboardedAt"] = d.pop("onboarded_at")
    d.pop("master_skill_id", None)
    return d


# ---------------------------------------------------------------- sources


def _row_to_source(row: sqlite3.Row) -> dict:
    d = dict(row)
    raw_config = json.loads(d.pop("config_js") or "{}")
    d["config"] = _decrypt_config(raw_config, _get_cipher())
    d["lastSync"] = d.pop("last_sync", None)
    d["resourceCount"] = d.pop("resource_count", 0)
    d["product"] = d.pop("product_id")
    return d


def add_source_methods(cls):
    """Mix-in style: add source helpers to Registry without re-declaring it."""

    def list_sources(self, product_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sources WHERE product_id = ? ORDER BY created_at DESC",
                (product_id,),
            ).fetchall()
        return [_row_to_source(r) for r in rows]

    def get_source(self, product_id: str, source_id_or_name: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE product_id = ? AND (id = ? OR name = ?)",
                (product_id, source_id_or_name, source_id_or_name),
            ).fetchone()
        return _row_to_source(row) if row else None

    def upsert_source(self, source: dict) -> None:
        import uuid as _uuid
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        sid = source.get("id") or f"src_{_uuid.uuid4().hex[:12]}"
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO sources
                   (id, product_id, name, type, status, config_js, last_sync,
                    resource_count, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    sid,
                    source["product"],
                    source["name"],
                    source["type"],
                    source.get("status", "connected"),
                    json.dumps(_encrypt_config(source.get("config", {}), _get_cipher())),
                    source.get("lastSync"),
                    int(source.get("resourceCount", 0)),
                    source.get("createdAt") or _dt.now(_UTC).isoformat(),
                ),
            )

    def delete_source(self, product_id: str, source_id_or_name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM sources WHERE product_id = ? AND (id = ? OR name = ?)",
                (product_id, source_id_or_name, source_id_or_name),
            )
            return cur.rowcount > 0

    def delete_product(self, product_id: str) -> dict[str, int]:
        """Delete product metadata plus source manifests/runs from registry DB."""
        with self._conn() as conn:
            counts = {
                "source_resources": conn.execute(
                    "SELECT COUNT(*) FROM source_resources WHERE product_id = ?",
                    (product_id,),
                ).fetchone()[0],
                "source_sync_runs": conn.execute(
                    "SELECT COUNT(*) FROM source_sync_runs WHERE product_id = ?",
                    (product_id,),
                ).fetchone()[0],
                "enrichment_jobs": conn.execute(
                    "SELECT COUNT(*) FROM enrichment_jobs WHERE product_id = ?",
                    (product_id,),
                ).fetchone()[0],
                "sources": conn.execute(
                    "SELECT COUNT(*) FROM sources WHERE product_id = ?",
                    (product_id,),
                ).fetchone()[0],
                "products": conn.execute(
                    "SELECT COUNT(*) FROM products WHERE id = ?",
                    (product_id,),
                ).fetchone()[0],
                "product_members": conn.execute(
                    "SELECT COUNT(*) FROM product_members WHERE product_id = ?",
                    (product_id,),
                ).fetchone()[0],
            }
            conn.execute("DELETE FROM source_resources WHERE product_id = ?", (product_id,))
            conn.execute("DELETE FROM source_sync_runs WHERE product_id = ?", (product_id,))
            conn.execute("DELETE FROM enrichment_jobs WHERE product_id = ?", (product_id,))
            conn.execute("DELETE FROM sources WHERE product_id = ?", (product_id,))
            conn.execute("DELETE FROM product_members WHERE product_id = ?", (product_id,))
            conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        return counts

    def grant_product_role(self, product_id: str, user_id: str, role: str) -> None:
        if role not in {"owner", "editor", "viewer"}:
            raise ValueError(f"unsupported product role: {role}")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO product_members (product_id, user_id, role, created_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(product_id, user_id) DO UPDATE SET role = excluded.role""",
                (product_id, user_id, role, _now_iso()),
            )

    def get_product_role(self, product_id: str, user_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT role FROM product_members WHERE product_id = ? AND user_id = ?",
                (product_id, user_id),
            ).fetchone()
        return str(row["role"]) if row else None

    def list_product_ids_for_user(self, user_id: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT product_id FROM product_members WHERE user_id = ? ORDER BY product_id",
                (user_id,),
            ).fetchall()
        return [str(r["product_id"]) for r in rows]

    def list_product_memberships(self, user_id: str) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT product_id, role FROM product_members WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return {str(r["product_id"]): str(r["role"]) for r in rows}

    cls.list_sources = list_sources
    cls.get_source = get_source
    cls.upsert_source = upsert_source
    cls.delete_source = delete_source
    cls.delete_product = delete_product
    cls.grant_product_role = grant_product_role
    cls.get_product_role = get_product_role
    cls.list_product_ids_for_user = list_product_ids_for_user
    cls.list_product_memberships = list_product_memberships
    return cls


Registry = add_source_methods(Registry)


# ---------------------------------------------------------------- sync manifest


def _row_to_resource_manifest(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["chunkIds"] = json.loads(d.pop("chunk_ids_js") or "[]")
    d["sizeBytes"] = d.pop("size_bytes")
    d["lastSeenSync"] = d.pop("last_seen_sync")
    d["indexedAt"] = d.pop("indexed_at")
    d["embeddingVersion"] = d.pop("embedding_version")
    d["enrichmentVersion"] = d.pop("enrichment_version", "")
    d["enrichmentStatus"] = d.pop("enrichment_status", "")
    d["contentHash"] = d.pop("content_hash")
    d["resourceUri"] = d.pop("resource_uri")
    d["sourceKey"] = d.pop("source_key")
    d["product"] = d.pop("product_id")
    return d


def add_manifest_methods(cls):
    """Add resource manifest helpers used by delta-safe ingest."""

    def list_resource_manifests(self, product_id: str, source_key: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM source_resources
                   WHERE product_id = ? AND source_key = ?
                   ORDER BY resource_uri""",
                (product_id, source_key),
            ).fetchall()
        return [_row_to_resource_manifest(r) for r in rows]

    def get_resource_manifest(
        self, product_id: str, source_key: str, resource_uri: str
    ) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM source_resources
                   WHERE product_id = ? AND source_key = ? AND resource_uri = ?""",
                (product_id, source_key, resource_uri),
            ).fetchone()
        return _row_to_resource_manifest(row) if row else None

    def upsert_resource_manifest(self, row: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO source_resources
                   (product_id, source_key, resource_uri, content_hash, mime, size_bytes,
                    last_seen_sync, chunk_ids_js, indexed_at, embedding_version,
                    enrichment_version, enrichment_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["product"],
                    row["sourceKey"],
                    row["resourceUri"],
                    row["contentHash"],
                    row.get("mime", ""),
                    row.get("sizeBytes"),
                    row["lastSeenSync"],
                    json.dumps(row.get("chunkIds", [])),
                    row["indexedAt"],
                    row.get("embeddingVersion", ""),
                    row.get("enrichmentVersion", ""),
                    row.get("enrichmentStatus", ""),
                ),
            )

    def delete_resource_manifest(
        self, product_id: str, source_key: str, resource_uri: str
    ) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """DELETE FROM source_resources
                   WHERE product_id = ? AND source_key = ? AND resource_uri = ?""",
                (product_id, source_key, resource_uri),
            )
            return cur.rowcount > 0

    def update_resource_enrichment(
        self,
        product_id: str,
        source_key: str,
        resource_uri: str,
        *,
        enrichment_version: str,
        enrichment_status: str,
    ) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE source_resources
                   SET enrichment_version = ?, enrichment_status = ?
                   WHERE product_id = ? AND source_key = ? AND resource_uri = ?""",
                (
                    enrichment_version,
                    enrichment_status,
                    product_id,
                    source_key,
                    resource_uri,
                ),
            )
            return cur.rowcount > 0

    def start_sync_run(self, product_id: str, source_key: str, started_at: str) -> str:
        import uuid as _uuid

        run_id = f"sync_{_uuid.uuid4().hex[:12]}"
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO source_sync_runs
                   (id, product_id, source_key, started_at, status)
                   VALUES (?,?,?,?,?)""",
                (run_id, product_id, source_key, started_at, "running"),
            )
        return run_id

    def finish_sync_run(
        self,
        run_id: str,
        *,
        finished_at: str,
        added: int,
        updated: int,
        removed: int,
        unchanged: int,
        status: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE source_sync_runs
                   SET finished_at = ?, added = ?, updated = ?, removed = ?,
                       unchanged = ?, status = ?
                   WHERE id = ?""",
                (finished_at, added, updated, removed, unchanged, status, run_id),
            )

    cls.list_resource_manifests = list_resource_manifests
    cls.get_resource_manifest = get_resource_manifest
    cls.upsert_resource_manifest = upsert_resource_manifest
    cls.delete_resource_manifest = delete_resource_manifest
    cls.update_resource_enrichment = update_resource_enrichment
    cls.start_sync_run = start_sync_run
    cls.finish_sync_run = finish_sync_run
    return cls


Registry = add_manifest_methods(Registry)


# ---------------------------------------------------------------- enrichment queue


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_enrichment_job(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["sizeBytes"] = d.pop("size_bytes")
    d["lastModified"] = d.pop("last_modified")
    d["contentHash"] = d.pop("content_hash")
    d["resourceUri"] = d.pop("resource_uri")
    d["sourceKey"] = d.pop("source_key")
    d["sourceId"] = d.pop("source_id")
    d["lastError"] = d.pop("last_error")
    d["createdAt"] = d.pop("created_at")
    d["updatedAt"] = d.pop("updated_at")
    d["product"] = d.pop("product_id")
    return d


def add_enrichment_job_methods(cls):
    """Add durable background enrichment job helpers."""

    def enqueue_enrichment_job(self, row: dict) -> None:
        now = _utc_now()
        job_id = (
            row.get("id")
            or f"{row['product']}:{row['sourceKey']}:{row['resourceUri']}"
        )
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO enrichment_jobs
                   (id, product_id, source_key, resource_uri, source_id, mime,
                    size_bytes, last_modified, content_hash, content, status,
                    attempts, last_error, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       source_id = excluded.source_id,
                       mime = excluded.mime,
                       size_bytes = excluded.size_bytes,
                       last_modified = excluded.last_modified,
                       content_hash = excluded.content_hash,
                       content = excluded.content,
                       status = 'pending',
                       attempts = 0,
                       last_error = NULL,
                       updated_at = excluded.updated_at""",
                (
                    job_id,
                    row["product"],
                    row["sourceKey"],
                    row["resourceUri"],
                    row["sourceId"],
                    row.get("mime", ""),
                    row.get("sizeBytes"),
                    row.get("lastModified"),
                    row["contentHash"],
                    row["content"],
                    "pending",
                    0,
                    None,
                    now,
                    now,
                ),
            )

    def claim_enrichment_job(self, *, max_attempts: int) -> dict | None:
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM enrichment_jobs
                   WHERE status = 'pending' AND attempts < ?
                   ORDER BY updated_at ASC
                   LIMIT 1""",
                (max_attempts,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE enrichment_jobs
                   SET status = 'running', attempts = attempts + 1, updated_at = ?
                   WHERE id = ?""",
                (now, row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM enrichment_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
        return _row_to_enrichment_job(claimed) if claimed else None

    def complete_enrichment_job(self, job_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM enrichment_jobs WHERE id = ?", (job_id,))
            return cur.rowcount > 0

    def fail_enrichment_job(self, job_id: str, *, error: str, max_attempts: int) -> None:
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT attempts FROM enrichment_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            status = "failed" if int(row["attempts"]) >= max_attempts else "pending"
            conn.execute(
                """UPDATE enrichment_jobs
                   SET status = ?, last_error = ?, updated_at = ?
                   WHERE id = ?""",
                (status, error[:1000], now, job_id),
            )

    def enrichment_job_counts(self, product_id: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM enrichment_jobs"
        args: list[str] = []
        if product_id:
            sql += " WHERE product_id = ?"
            args.append(product_id)
        sql += " GROUP BY status"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        counts = {"pending": 0, "running": 0, "failed": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        return counts

    def reset_running_enrichment_jobs(self) -> int:
        now = _utc_now()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE enrichment_jobs
                   SET status = 'pending', updated_at = ?
                   WHERE status = 'running'""",
                (now,),
            )
            return cur.rowcount

    cls.enqueue_enrichment_job = enqueue_enrichment_job
    cls.claim_enrichment_job = claim_enrichment_job
    cls.complete_enrichment_job = complete_enrichment_job
    cls.fail_enrichment_job = fail_enrichment_job
    cls.enrichment_job_counts = enrichment_job_counts
    cls.reset_running_enrichment_jobs = reset_running_enrichment_jobs
    return cls


Registry = add_enrichment_job_methods(Registry)
