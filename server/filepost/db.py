"""Схема и доступ к SQLite. Раздел 2.3 архитектуры."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

# AUTOINCREMENT у events обязателен: без него SQLite переиспользует id после
# удаления строк, и клиент с курсором на старом значении молча пропустит события.
SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS stations (
    id              INTEGER PRIMARY KEY,
    display_name    TEXT NOT NULL UNIQUE,
    machine_name    TEXT,
    secret_hash     TEXT NOT NULL,
    last_ip         TEXT,
    client_version  TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT
);

CREATE TABLE IF NOT EXISTS enrollment_codes (
    code        TEXT PRIMARY KEY,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    used_by     INTEGER REFERENCES stations(id),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token       TEXT PRIMARY KEY,
    station_id  INTEGER NOT NULL REFERENCES stations(id),
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_station ON tokens(station_id);

CREATE TABLE IF NOT EXISTS messages (
    id                 INTEGER PRIMARY KEY,
    sender_id          INTEGER NOT NULL REFERENCES stations(id),
    subject            TEXT NOT NULL DEFAULT '',
    body               TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'draft',
    created_at         TEXT NOT NULL,
    sent_at            TEXT,
    deleted_by_sender  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id, status);

CREATE TABLE IF NOT EXISTS message_recipients (
    id                    INTEGER PRIMARY KEY,
    message_id            INTEGER NOT NULL REFERENCES messages(id),
    recipient_id          INTEGER NOT NULL REFERENCES stations(id),
    is_read               INTEGER NOT NULL DEFAULT 0,
    downloaded_at         TEXT,
    deleted_by_recipient  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(message_id, recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_recipients_station ON message_recipients(recipient_id);

CREATE TABLE IF NOT EXISTS attachments (
    id             INTEGER PRIMARY KEY,
    message_id     INTEGER NOT NULL REFERENCES messages(id),
    original_name  TEXT NOT NULL,
    storage_path   TEXT,
    size           INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'uploading',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_attachments_state ON attachments(state);

CREATE TABLE IF NOT EXISTS upload_sessions (
    id              TEXT PRIMARY KEY,
    attachment_id   INTEGER NOT NULL REFERENCES attachments(id),
    station_id      INTEGER NOT NULL REFERENCES stations(id),
    total_size      INTEGER NOT NULL,
    chunk_size      INTEGER NOT NULL,
    received_chunks TEXT NOT NULL DEFAULT '[]',
    state           TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_state ON upload_sessions(state);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id  INTEGER NOT NULL REFERENCES stations(id),
    type        TEXT NOT NULL,
    object_id   INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_station ON events(station_id, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id  INTEGER REFERENCES stations(id),
    action      TEXT NOT NULL,
    object_id   INTEGER,
    details     TEXT,
    timestamp   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);
"""


class Database:
    """Одно соединение на поток: SQLite-объекты не переносятся между потоками."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        return int(self.execute(sql, params).lastrowid or 0)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def backup_to(self, target: Path) -> None:
        """Согласованный снимок живой базы. Копировать файл в режиме WAL нельзя (2.13)."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(target)
        try:
            with self._write_lock:
                self.conn.backup(dest)
        finally:
            dest.close()
