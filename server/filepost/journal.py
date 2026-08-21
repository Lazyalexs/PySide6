"""Журнал событий для синхронизации и аудит-журнал. Раздел 2.3.

Разведены намеренно: audit_log пишется от лица станции, совершившей действие,
и не чистится никогда; events адресован станции, которой надо узнать об изменении,
и живёт events_retention_days.
"""

from __future__ import annotations

import json
from typing import Any

from .db import Database
from .util import utcnow

NEW_MESSAGE = "new_message"
READ = "read"
DOWNLOADED = "downloaded"
DELETED = "deleted"
RENAMED = "renamed"
REVOKED = "revoked"
#: предупреждение отправителю о скором автоудалении (2.6, notify_sender_before_days)
RETENTION_WARNING = "retention_warning"


def emit(db: Database, station_id: int, type_: str, object_id: int | None = None) -> int:
    return db.insert(
        "INSERT INTO events (station_id, type, object_id, created_at) VALUES (?,?,?,?)",
        (station_id, type_, object_id, utcnow()),
    )


def audit(
    db: Database,
    station_id: int | None,
    action: str,
    object_id: int | None = None,
    **details: Any,
) -> None:
    db.execute(
        "INSERT INTO audit_log (station_id, action, object_id, details, timestamp)"
        " VALUES (?,?,?,?,?)",
        (
            station_id,
            action,
            object_id,
            json.dumps(details, ensure_ascii=False) if details else None,
            utcnow(),
        ),
    )


def events_since(db: Database, station_id: int, since: int, limit: int = 500) -> list[dict]:
    rows = db.query(
        "SELECT id, type, object_id, created_at FROM events"
        " WHERE station_id = ? AND id > ? ORDER BY id LIMIT ?",
        (station_id, since, limit),
    )
    return [dict(r) for r in rows]


def resync_required(db: Database, since: int) -> bool:
    """Курсор проверяется с двух сторон (2.3).

    Меньше минимального id — станция не была в сети дольше срока хранения журнала.
    Больше максимального — журнал откатился назад, что происходит после
    восстановления БД из резервной копии (2.13). Без второй проверки такая станция
    замолчала бы навсегда: её курсор недостижим.
    """
    if since <= 0:
        return False
    max_id = db.scalar("SELECT MAX(id) FROM events")
    if max_id is not None and since > max_id:
        return True
    min_id = db.scalar("SELECT MIN(id) FROM events")
    return min_id is not None and since < min_id - 1
