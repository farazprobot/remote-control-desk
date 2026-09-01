"""Persistent session-key validation shared by the Telegram bot and relay."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from pathlib import Path


class KeyStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or os.environ.get("SESSION_KEYS_DB", "data/session_keys.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._init_db()

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_hash TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL DEFAULT 'legacy',
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    revoked_at TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(session_keys)").fetchall()
            }
            if "role" not in columns:
                conn.execute(
                    "ALTER TABLE session_keys ADD COLUMN role TEXT NOT NULL DEFAULT 'legacy'"
                )

    def create(self, role: str, label: str = "") -> tuple[int, str]:
        if role not in {"master", "agent"}:
            raise ValueError("role must be master or agent")
        raw_key = "RCD-" + secrets.token_urlsafe(24).replace("-", "").replace("_", "")
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO session_keys (key_hash, role, label) VALUES (?, ?, ?)",
                (self._hash(raw_key), role, label[:80]),
            )
            return int(cursor.lastrowid), raw_key

    def authenticate(self, raw_key: str, role: str | None = None) -> dict | None:
        """Return non-secret key metadata, optionally restricted to a role."""
        if not raw_key or len(raw_key) > 200:
            return None
        if role is not None and role not in {"master", "agent"}:
            return None
        digest = self._hash(raw_key)
        with self.lock, self._connect() as conn:
            if role is None:
                row = conn.execute(
                    """
                    SELECT id, key_hash, role, label, created_at
                    FROM session_keys
                    WHERE key_hash = ? AND revoked_at IS NULL
                    """,
                    (digest,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id, key_hash, role, label, created_at
                    FROM session_keys
                    WHERE key_hash = ? AND role = ? AND revoked_at IS NULL
                    """,
                    (digest, role),
                ).fetchone()
        if not row or not hmac.compare_digest(row["key_hash"], digest):
            return None
        return {
            "id": row["id"],
            "key_hash": row["key_hash"],
            "role": row["role"],
            "label": row["label"],
            "created_at": row["created_at"],
        }

    def validate(self, raw_key: str, role: str | None = None) -> bool:
        """Compatibility helper; role is required for new role-bound keys."""
        if role is None:
            return False
        return self.authenticate(raw_key, role) is not None

    def list_keys(self) -> list[dict]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, role, label, created_at, revoked_at
                FROM session_keys
                ORDER BY id DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def revoke(self, key_id: int) -> bool:
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE session_keys
                SET revoked_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revoked_at IS NULL
                """,
                (key_id,),
            )
            return cursor.rowcount == 1