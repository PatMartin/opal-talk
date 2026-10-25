"""SQLite persistence for engine configuration, voice registrations, profiles and settings."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS engines (
    engine_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    options TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voices (
    voice_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    engine_id TEXT NOT NULL,
    engine_voice_ref TEXT NOT NULL,
    language TEXT,
    enabled INTEGER NOT NULL,
    native_sample_rate INTEGER,
    engine_options TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    is_active INTEGER NOT NULL,
    voice_id TEXT NOT NULL,
    speed REAL NOT NULL,
    volume REAL NOT NULL,
    output_format TEXT NOT NULL,
    sample_rate INTEGER,
    engine_options TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

JSON_COLUMNS = {"options", "engine_options", "metadata"}
BOOL_COLUMNS = {"enabled", "is_active"}


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if key in JSON_COLUMNS:
            value = json.loads(value)
        elif key in BOOL_COLUMNS:
            value = bool(value)
        out[key] = value
    return out


def _encode(values: dict[str, Any]) -> dict[str, Any]:
    return {k: json.dumps(v) if k in JSON_COLUMNS else (int(v) if k in BOOL_COLUMNS else v) for k, v in values.items()}


class Store:
    """Thread-safe thin repository over a single SQLite connection."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.created = str(db_path) == ":memory:" or not self.db_path.exists()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Write transaction. A commit that actually modified rows bumps `config_revision`
        (the store-wide mutation counter backing ETag/If-Match in contract 1.2.0)."""
        with self._lock:
            changes_before = self._conn.total_changes
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            if self._conn.total_changes != changes_before:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('config_revision', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
                )
            self._conn.execute("COMMIT")

    def config_revision(self) -> int:
        """Monotonic counter bumped by every committed write transaction (0 if none yet)."""
        row = self._one("SELECT value FROM meta WHERE key = 'config_revision'")
        return int(row["value"]) if row else 0

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [_decode(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
        enc = _encode(values)
        cols = ", ".join(enc)
        marks = ", ".join("?" for _ in enc)
        return conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(enc.values())).lastrowid

    @staticmethod
    def _update(conn: sqlite3.Connection, table: str, key_col: str, key: Any, values: dict[str, Any]) -> None:
        enc = _encode(values)
        assignments = ", ".join(f"{c} = ?" for c in enc)
        conn.execute(f"UPDATE {table} SET {assignments} WHERE {key_col} = ?", (*enc.values(), key))

    # -- engines ------------------------------------------------------------

    def get_engine_state(self, engine_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM engines WHERE engine_id = ?", (engine_id,))

    def list_engine_states(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM engines ORDER BY engine_id")

    def upsert_engine_state(self, engine_id: str, enabled: bool, options: dict[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO engines (engine_id, enabled, options) VALUES (?, ?, ?) "
                "ON CONFLICT(engine_id) DO UPDATE SET enabled = excluded.enabled, options = excluded.options",
                (engine_id, int(enabled), json.dumps(options)),
            )

    # -- voices -------------------------------------------------------------

    def list_voices(self, engine_id: str | None = None, enabled: bool | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM voices WHERE 1 = 1", []
        if engine_id is not None:
            sql += " AND engine_id = ?"
            params.append(engine_id)
        if enabled is not None:
            sql += " AND enabled = ?"
            params.append(int(enabled))
        return self._all(sql + " ORDER BY voice_id", tuple(params))

    def get_voice(self, voice_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM voices WHERE voice_id = ?", (voice_id,))

    def insert_voice(self, values: dict[str, Any]) -> None:
        with self.transaction() as conn:
            self._insert(conn, "voices", values)

    def update_voice(self, voice_id: str, values: dict[str, Any]) -> None:
        with self.transaction() as conn:
            self._update(conn, "voices", "voice_id", voice_id, values)

    def delete_voice(self, voice_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM voices WHERE voice_id = ?", (voice_id,))

    # -- profiles -------------------------------------------------------------

    def list_profiles(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM profiles ORDER BY id")

    def get_profile(self, profile_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM profiles WHERE id = ?", (profile_id,))

    def get_profile_by_name(self, name: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM profiles WHERE name = ?", (name,))

    def active_profile(self) -> dict[str, Any] | None:
        return self._one("SELECT * FROM profiles WHERE is_active = 1 ORDER BY id LIMIT 1")

    def profiles_using_voice(self, voice_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM profiles WHERE voice_id = ? ORDER BY id", (voice_id,))

    def insert_profile(self, values: dict[str, Any]) -> int:
        with self.transaction() as conn:
            if values.get("is_active"):
                conn.execute("UPDATE profiles SET is_active = 0")
            return self._insert(conn, "profiles", values)

    def update_profile(self, profile_id: int, values: dict[str, Any]) -> None:
        with self.transaction() as conn:
            if values.get("is_active"):
                conn.execute("UPDATE profiles SET is_active = 0 WHERE id != ?", (profile_id,))
            self._update(conn, "profiles", "id", profile_id, values)

    def delete_profile(self, profile_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))

    # -- settings -----------------------------------------------------------

    def list_settings(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM settings ORDER BY key")

    def get_setting(self, key: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM settings WHERE key = ?", (key,))

    def upsert_setting(self, key: str, value: str) -> dict[str, Any]:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, now_iso()),
            )
        return self.get_setting(key)  # type: ignore[return-value]

    def delete_setting(self, key: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    # -- bulk replace (import/restore) -----------------------------------------

    def replace_all(
        self,
        engines: dict[str, dict[str, Any]] | None,
        voices: list[dict[str, Any]] | None,
        profiles: list[dict[str, Any]] | None,
        settings: dict[str, str] | None,
    ) -> None:
        """Atomically apply each provided section; `None` leaves a section untouched.

        Voices, profiles and settings are fully replaced (deleted then re-inserted);
        engine entries are merged — upserted `enabled`/`options` for installed engines,
        state for unlisted engines is preserved.
        """
        stamp = now_iso()
        with self.transaction() as conn:
            if engines is not None:
                for engine_id, state in engines.items():
                    conn.execute(
                        "INSERT INTO engines (engine_id, enabled, options) VALUES (?, ?, ?) "
                        "ON CONFLICT(engine_id) DO UPDATE SET enabled = excluded.enabled, options = excluded.options",
                        (engine_id, int(state["enabled"]), json.dumps(state["options"])),
                    )
            if profiles is not None:
                conn.execute("DELETE FROM profiles")
            if voices is not None:
                conn.execute("DELETE FROM voices")
                for voice in voices:
                    self._insert(conn, "voices", {"created_at": stamp, "updated_at": stamp, **voice})
            if profiles is not None:
                for profile in profiles:
                    self._insert(conn, "profiles", {"created_at": stamp, "updated_at": stamp, **profile})
            if settings is not None:
                conn.execute("DELETE FROM settings")
                for key, value in settings.items():
                    conn.execute(
                        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)", (key, value, stamp)
                    )
