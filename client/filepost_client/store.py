"""LocalStore: client.db. Раздел 3.3.

Состояние очереди лежит здесь, а не в памяти: закрыли приложение на середине
пятигигабайтной заливки — после запуска она продолжится, а не начнётся заново.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    folder        TEXT NOT NULL,          -- inbox | sent
    sender        TEXT,
    subject       TEXT,
    body          TEXT,
    sent_at       TEXT,
    is_read       INTEGER DEFAULT 0,
    downloaded_at TEXT,
    recipients    TEXT,                   -- JSON
    attachments   TEXT,                   -- JSON
    total_size    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stations (
    station_id     INTEGER PRIMARY KEY,
    display_name   TEXT,
    online         INTEGER DEFAULT 0,
    last_seen_at   TEXT,
    client_version TEXT
);

CREATE TABLE IF NOT EXISTS transfers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    direction      TEXT NOT NULL,          -- upload | download
    state          TEXT NOT NULL,          -- queued|active|verifying|paused|done|error
    file_path      TEXT,
    file_name      TEXT NOT NULL,
    size           INTEGER NOT NULL DEFAULT 0,
    transferred    INTEGER NOT NULL DEFAULT 0,
    sha256         TEXT,
    message_id     INTEGER,
    attachment_id  INTEGER,
    upload_id      TEXT,
    peer           TEXT,
    error          TEXT,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_transfers_state ON transfers(state);
"""


class LocalStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        self.init_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ курсор

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def last_event_id(self) -> int:
        return int(self.get_meta("last_event_id", "0"))

    def set_last_event_id(self, value: int) -> None:
        """Курсор сохраняется ТОЛЬКО после применения событий к локальной БД (3.5).

        Сохранить раньше — значит потерять события при падении клиента между двумя
        операциями. Повторно применённое событие безвредно, потерянное — это
        не пришедшее письмо.
        """
        self.set_meta("last_event_id", str(value))

    # ------------------------------------------------------------------ сообщения

    def upsert_messages(self, folder: str, items: list[dict]) -> None:
        for item in items:
            self.execute(
                "INSERT INTO messages (id, folder, sender, subject, body, sent_at,"
                " is_read, downloaded_at, recipients, attachments, total_size)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET folder=excluded.folder,"
                " sender=excluded.sender, subject=excluded.subject, body=excluded.body,"
                " sent_at=excluded.sent_at, is_read=excluded.is_read,"
                " downloaded_at=excluded.downloaded_at, recipients=excluded.recipients,"
                " attachments=excluded.attachments, total_size=excluded.total_size",
                (
                    item["id"],
                    folder,
                    item.get("sender"),
                    item.get("subject", ""),
                    item.get("body", ""),
                    item.get("sent_at"),
                    int(bool(item.get("is_read"))),
                    item.get("downloaded_at"),
                    json.dumps(item.get("recipients", []), ensure_ascii=False),
                    json.dumps(item.get("attachments", []), ensure_ascii=False),
                    item.get("total_size", 0),
                ),
            )

    def replace_folder(self, folder: str, items: list[dict]) -> None:
        """Полная замена — после resync_required (3.5)."""
        self.execute("DELETE FROM messages WHERE folder = ?", (folder,))
        self.upsert_messages(folder, items)

    def messages(self, folder: str) -> list[dict]:
        rows = self.query(
            "SELECT * FROM messages WHERE folder = ? ORDER BY sent_at DESC", (folder,)
        )
        return [self._row_to_message(r) for r in rows]

    def message(self, message_id: int) -> dict | None:
        row = self.one("SELECT * FROM messages WHERE id = ?", (message_id,))
        return self._row_to_message(row) if row else None

    def remove_message(self, message_id: int) -> None:
        self.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    def unread_count(self) -> int:
        row = self.one(
            "SELECT COUNT(*) AS n FROM messages WHERE folder = 'inbox' AND is_read = 0"
        )
        return row["n"] if row else 0

    def mark_read_local(self, message_id: int) -> None:
        self.execute("UPDATE messages SET is_read = 1 WHERE id = ?", (message_id,))

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["recipients"] = json.loads(item.get("recipients") or "[]")
        item["attachments"] = json.loads(item.get("attachments") or "[]")
        item["is_read"] = bool(item.get("is_read"))
        return item

    # ------------------------------------------------------------------ станции

    def replace_stations(self, items: list[dict]) -> None:
        self.execute("DELETE FROM stations", ())
        for item in items:
            self.execute(
                "INSERT INTO stations (station_id, display_name, online, last_seen_at,"
                " client_version) VALUES (?,?,?,?,?)",
                (
                    item["station_id"],
                    item["display_name"],
                    int(bool(item.get("online"))),
                    item.get("last_seen_at"),
                    item.get("client_version"),
                ),
            )

    def stations(self, exclude_self: int = 0) -> list[dict]:
        rows = self.query(
            "SELECT * FROM stations WHERE station_id != ? ORDER BY display_name",
            (exclude_self,),
        )
        return [dict(r) | {"online": bool(r["online"])} for r in rows]

    # ------------------------------------------------------------------ передачи

    def add_transfer(self, **fields) -> int:
        keys = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        cur = self.execute(
            f"INSERT INTO transfers ({keys}) VALUES ({marks})", list(fields.values())
        )
        return int(cur.lastrowid or 0)

    def update_transfer(self, transfer_id: int, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE transfers SET {sets} WHERE id = ?", [*fields.values(), transfer_id]
        )

    def transfer(self, transfer_id: int) -> dict | None:
        row = self.one("SELECT * FROM transfers WHERE id = ?", (transfer_id,))
        return dict(row) if row else None

    def transfers(self, states: list[str] | None = None) -> list[dict]:
        if states:
            marks = ",".join("?" * len(states))
            rows = self.query(
                f"SELECT * FROM transfers WHERE state IN ({marks}) ORDER BY id", states
            )
        else:
            rows = self.query("SELECT * FROM transfers ORDER BY id DESC LIMIT 200")
        return [dict(r) for r in rows]

    def pending_transfers(self) -> list[dict]:
        """Что подхватывать после перезапуска приложения."""
        return self.transfers(["queued", "active", "verifying"])

    def remove_transfer(self, transfer_id: int) -> None:
        self.execute("DELETE FROM transfers WHERE id = ?", (transfer_id,))

    def clear_finished(self) -> None:
        self.execute("DELETE FROM transfers WHERE state IN ('done','error')", ())
