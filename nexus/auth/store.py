"""SQLite auth store for deployed Nexus instances."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

SESSION_COOKIE = "nexus_session"
CSRF_COOKIE = "nexus_csrf"
SESSION_TTL_DAYS = 14
ROLES = {"admin", "editor", "viewer"}

_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_users (
    id              TEXT PRIMARY KEY,
    auth_sub        TEXT,
    email           TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL DEFAULT '',
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'viewer',
    status          TEXT NOT NULL DEFAULT 'approved',
    created_at      TEXT NOT NULL,
    approved_at     TEXT,
    revoked_at      TEXT,
    last_login_at   TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    csrf_token      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    revoked_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_token
    ON auth_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
    ON auth_sessions(user_id);
CREATE TABLE IF NOT EXISTS auth_access_requests (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    decided_by      TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_access_requests_status
    ON auth_access_requests(status, created_at DESC);

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    bucket      TEXT NOT NULL,
    subject     TEXT NOT NULL,
    ts          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_rate_limits_bucket
    ON auth_rate_limits(bucket, subject, ts);
"""


@dataclass(frozen=True)
class LoginResult:
    user: dict
    session_token: str
    csrf_token: str
    expires_at: str


class AuthError(RuntimeError):
    pass


class AuthStore:
    def __init__(self, db_path: Path, *, secret_key: str | None = None):
        self.db_path = Path(db_path)
        self.secret_key = secret_key or os.getenv("NEXUS_SECRET_KEY") or ""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            _ensure_columns(conn)
        self.bootstrap_admin_from_env()

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

    # ------------------------------------------------------------ users

    def bootstrap_admin_from_env(self) -> None:
        email = _normalize_email(os.getenv("NEXUS_BOOTSTRAP_ADMIN_EMAIL") or "")
        password = os.getenv("NEXUS_BOOTSTRAP_ADMIN_PASSWORD") or ""
        if not email or not password:
            return
        if self.get_user_by_email(email):
            return
        self.create_user(email=email, password=password, role="admin", status="approved")

    def create_user(
        self,
        *,
        email: str,
        password: str,
        name: str = "",
        role: str = "viewer",
        status: str = "approved",
    ) -> dict:
        email = _normalize_email(email)
        if role not in ROLES:
            raise AuthError(f"unsupported role: {role}")
        if len(password) < 12:
            raise AuthError("password must be at least 12 characters")
        now = _now()
        user_id = secrets.token_urlsafe(16)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO auth_users
                   (id, email, name, password_hash, role, status, created_at, approved_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    email,
                    name.strip(),
                    hash_password(password),
                    role,
                    status,
                    now,
                    now if status == "approved" else None,
                ),
            )
        user = self.get_user(user_id)
        if user is None:
            raise AuthError("failed to create user")
        return user

    def get_or_create_auth0_user(
        self, *, auth_sub: str, email: str, name: str = ""
    ) -> dict:
        auth_sub = auth_sub.strip()
        email = _normalize_email(email)
        name = name.strip()
        if not auth_sub:
            raise AuthError("auth_sub is required")
        by_sub = self.get_user_by_auth_sub(auth_sub)
        if by_sub:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE auth_users SET email = ?, name = ?, last_login_at = ? WHERE id = ?",
                    (email, name or by_sub.get("name") or email, _now(), by_sub["id"]),
                )
            return self.get_user(by_sub["id"]) or by_sub

        existing = self.get_user_by_email(email)
        if existing:
            bootstrap = _normalize_email(os.getenv("NEXUS_BOOTSTRAP_ADMIN_EMAIL") or "")
            promote = bool(bootstrap and email == bootstrap)
            now = _now()
            with self._conn() as conn:
                conn.execute(
                    """UPDATE auth_users
                       SET auth_sub = ?,
                           name = ?,
                           last_login_at = ?,
                           role = CASE WHEN ? THEN 'admin' ELSE role END,
                           status = CASE WHEN ? THEN 'approved' ELSE status END,
                           approved_at = CASE WHEN ? THEN ? ELSE approved_at END,
                           revoked_at = CASE WHEN ? THEN NULL ELSE revoked_at END
                       WHERE id = ?""",
                    (
                        auth_sub,
                        name or existing.get("name") or email,
                        now,
                        promote,
                        promote,
                        promote,
                        now,
                        promote,
                        existing["id"],
                    ),
                )
            return self.get_user(existing["id"]) or existing

        bootstrap = _normalize_email(os.getenv("NEXUS_BOOTSTRAP_ADMIN_EMAIL") or "")
        role = "admin" if bootstrap and email == bootstrap else "viewer"
        status = "approved" if role == "admin" else "pending"
        now = _now()
        user_id = secrets.token_urlsafe(16)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO auth_users
                   (id, auth_sub, email, name, password_hash, role, status, created_at, approved_at,
                    last_login_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    auth_sub,
                    email,
                    name or email,
                    "",
                    role,
                    status,
                    now,
                    now if status == "approved" else None,
                    now,
                ),
            )
        user = self.get_user(user_id)
        if user is None:
            raise AuthError("failed to create Auth0 user")
        return user

    def get_user(self, user_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM auth_users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row) if row else None

    def get_user_by_email(self, email: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM auth_users WHERE email = ?", (_normalize_email(email),)
            ).fetchone()
        return _row_to_user(row) if row else None

    def get_user_by_auth_sub(self, auth_sub: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM auth_users WHERE auth_sub = ?", (auth_sub,)
            ).fetchone()
        return _row_to_user(row) if row else None

    def list_users(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM auth_users ORDER BY created_at DESC").fetchall()
        return [_row_to_user(r) for r in rows]

    def approve_user(self, email: str, *, role: str = "viewer") -> dict:
        email = _normalize_email(email)
        if role not in ROLES:
            raise AuthError(f"unsupported role: {role}")
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE auth_users
                   SET status = 'approved', role = ?, approved_at = ?, revoked_at = NULL
                   WHERE email = ?""",
                (role, now, email),
            )
        if cur.rowcount == 0:
            raise AuthError("user not found")
        user = self.get_user_by_email(email)
        if user is None:
            raise AuthError("user not found")
        return user

    def revoke_user(self, email: str) -> dict:
        email = _normalize_email(email)
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE auth_users SET status = 'revoked', revoked_at = ? WHERE email = ?",
                (now, email),
            )
            row = conn.execute("SELECT id FROM auth_users WHERE email = ?", (email,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (now, row["id"]),
                )
        if cur.rowcount == 0:
            raise AuthError("user not found")
        user = self.get_user_by_email(email)
        if user is None:
            raise AuthError("user not found")
        return user

    # ------------------------------------------------------------ login/session

    def login(self, *, email: str, password: str) -> LoginResult:
        user = self.get_user_by_email(email)
        if not user or user["status"] != "approved":
            raise AuthError("invalid credentials")
        try:
            ok = _HASHER.verify(user["password_hash"], password)
        except VerifyMismatchError as e:
            raise AuthError("invalid credentials") from e
        if not ok:
            raise AuthError("invalid credentials")
        if _HASHER.check_needs_rehash(user["password_hash"]):
            with self._conn() as conn:
                conn.execute(
                    "UPDATE auth_users SET password_hash = ? WHERE id = ?",
                    (hash_password(password), user["id"]),
                )
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = datetime.now(UTC)
        expires = now + timedelta(days=SESSION_TTL_DAYS)
        session_id = secrets.token_urlsafe(16)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO auth_sessions
                   (id, user_id, token_hash, csrf_token, created_at, expires_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    session_id,
                    user["id"],
                    self.hash_session_token(token),
                    csrf,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            conn.execute(
                "UPDATE auth_users SET last_login_at = ? WHERE id = ?",
                (now.isoformat(), user["id"]),
            )
        fresh = self.get_user(user["id"]) or user
        return LoginResult(
            user=fresh,
            session_token=token,
            csrf_token=csrf,
            expires_at=expires.isoformat(),
        )

    def user_for_session(self, token: str) -> tuple[dict, dict] | None:
        if not token:
            return None
        token_hash = self.hash_session_token(token)
        now = _now()
        with self._conn() as conn:
            row = conn.execute(
                """SELECT s.*, u.email, u.role, u.status
                   FROM auth_sessions s
                   JOIN auth_users u ON u.id = s.user_id
                   WHERE s.token_hash = ?
                     AND s.revoked_at IS NULL
                     AND s.expires_at > ?
                     AND u.status = 'approved'""",
                (token_hash, now),
            ).fetchone()
        if not row:
            return None
        session = dict(row)
        user = self.get_user(session["user_id"])
        return (user, session) if user else None

    def revoke_session(self, token: str) -> None:
        if not token:
            return
        with self._conn() as conn:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ?",
                (_now(), self.hash_session_token(token)),
            )

    def hash_session_token(self, token: str) -> str:
        if not self.secret_key:
            raise AuthError("NEXUS_SECRET_KEY is required for auth sessions")
        digest = hmac.new(
            self.secret_key.encode(), token.encode(), hashlib.sha256
        ).hexdigest()
        return digest

    # ------------------------------------------------------------ access requests

    def request_access(self, *, email: str, name: str = "", reason: str = "") -> dict:
        request_id = secrets.token_urlsafe(16)
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO auth_access_requests
                   (id, email, name, reason, status, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (request_id, _normalize_email(email), name.strip(), reason.strip(), "pending", now),
            )
        return self.get_access_request(request_id) or {"id": request_id}

    def get_access_request(self, request_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM auth_access_requests WHERE id = ?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_access_requests(self, *, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM auth_access_requests WHERE 1=1"
        args: list[str] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def decide_access_request(
        self,
        request_id: str,
        *,
        status: str,
        decided_by: str,
        password: str | None = None,
        role: str = "viewer",
        require_password: bool = True,
    ) -> dict:
        if status not in {"approved", "rejected"}:
            raise AuthError("status must be approved or rejected")
        req = self.get_access_request(request_id)
        if not req:
            raise AuthError("request not found")
        now = _now()
        if role not in ROLES:
            raise AuthError(f"unsupported role: {role}")
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if status == "approved":
                existing = conn.execute(
                    "SELECT * FROM auth_users WHERE email = ?",
                    (_normalize_email(req["email"]),),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE auth_users
                           SET status = 'approved',
                               role = ?,
                               approved_at = ?,
                               revoked_at = NULL
                           WHERE email = ?""",
                        (role, now, _normalize_email(req["email"])),
                    )
                else:
                    if require_password and not password:
                        raise AuthError("password is required to approve a new user")
                    if len(password or "") < 12:
                        raise AuthError("password must be at least 12 characters")
                    conn.execute(
                        """INSERT INTO auth_users
                           (id, email, name, password_hash, role, status, created_at, approved_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            secrets.token_urlsafe(16),
                            _normalize_email(req["email"]),
                            str(req.get("name") or "").strip(),
                            hash_password(password or secrets.token_urlsafe(24)),
                            role,
                            "approved",
                            now,
                            now,
                        ),
                    )
            else:
                if status != "rejected":
                    raise AuthError("status must be approved or rejected")
            conn.execute(
                """UPDATE auth_access_requests
                   SET status = ?, decided_at = ?, decided_by = ?
                   WHERE id = ?""",
                (status, now, decided_by, request_id),
            )
            conn.execute("COMMIT")
        return self.get_access_request(request_id) or req

    # ------------------------------------------------------------ rate limits

    def check_rate_limit(
        self, *, bucket: str, subject: str, limit: int, window_s: int
    ) -> None:
        now_dt = datetime.now(UTC)
        cutoff = (now_dt - timedelta(seconds=window_s)).isoformat()
        now = now_dt.isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM auth_rate_limits WHERE bucket = ? AND subject = ? AND ts <= ?",
                (bucket, subject, cutoff),
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM auth_rate_limits WHERE bucket = ? AND subject = ? AND ts > ?",
                (bucket, subject, cutoff),
            ).fetchone()[0]
            if count >= limit:
                raise AuthError("rate limit exceeded")
            conn.execute(
                "INSERT INTO auth_rate_limits (bucket, subject, ts) VALUES (?,?,?)",
                (bucket, subject, now),
            )
            conn.execute("COMMIT")


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def _row_to_user(row: sqlite3.Row) -> dict:
    return dict(row)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(auth_users)").fetchall()}
    if "auth_sub" not in cols:
        conn.execute("ALTER TABLE auth_users ADD COLUMN auth_sub TEXT")
    if "name" not in cols:
        conn.execute("ALTER TABLE auth_users ADD COLUMN name TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_users_auth_sub "
        "ON auth_users(auth_sub) WHERE auth_sub IS NOT NULL AND auth_sub != ''"
    )
