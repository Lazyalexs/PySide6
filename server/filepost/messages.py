"""Сообщения, вложения, статусы доставки. Разделы 2.3, 2.4, 2.5."""

from __future__ import annotations

import json
import uuid

from .config import Config
from .db import Database
from .journal import DELETED, DOWNLOADED, NEW_MESSAGE, READ, audit, emit
from .storage import StorageError, check_space
from .util import utcnow


def create_message(
    db: Database, cfg: Config, sender_id: int, subject: str, body: str, recipients: list[int]
) -> int:
    if not recipients:
        raise StorageError("Не указан ни один получатель", 400)
    if len(recipients) > cfg.limits.max_recipients_per_message:
        raise StorageError(
            f"Больше {cfg.limits.max_recipients_per_message} получателей за раз нельзя", 400
        )

    unique = list(dict.fromkeys(recipients))
    known = {
        r["id"]
        for r in db.query(
            f"SELECT id FROM stations WHERE is_active = 1 AND id IN "
            f"({','.join('?' * len(unique))})",
            unique,
        )
    }
    unknown = [r for r in unique if r not in known]
    if unknown:
        raise StorageError(f"Получатели не найдены или отключены: {unknown}", 400)

    message_id = db.insert(
        "INSERT INTO messages (sender_id, subject, body, status, created_at)"
        " VALUES (?,?,?,'draft',?)",
        (sender_id, subject or "", body or "", utcnow()),
    )
    for recipient_id in unique:
        db.execute(
            "INSERT INTO message_recipients (message_id, recipient_id) VALUES (?,?)",
            (message_id, recipient_id),
        )
    audit(db, sender_id, "message.create", message_id, recipients=unique)
    return message_id


def init_attachment(
    db: Database,
    cfg: Config,
    station_id: int,
    message_id: int,
    name: str,
    size: int,
    sha256: str,
) -> dict:
    message = _owned_draft(db, station_id, message_id)

    count = db.scalar("SELECT COUNT(*) FROM attachments WHERE message_id = ?", (message_id,))
    if count >= cfg.limits.max_attachments_per_message:
        raise StorageError(
            f"Больше {cfg.limits.max_attachments_per_message} вложений в сообщении нельзя", 400
        )

    active = db.scalar(
        "SELECT COUNT(*) FROM upload_sessions WHERE station_id = ? AND state = 'active'",
        (station_id,),
    )
    if active >= cfg.limits.max_parallel_uploads_per_user:
        raise StorageError(
            f"Одновременно можно загружать не больше "
            f"{cfg.limits.max_parallel_uploads_per_user} файлов",
            429,
        )

    # Отказ по месту приходит здесь, а не на середине пятигигабайтной заливки.
    check_space(db, cfg, size)

    attachment_id = db.insert(
        "INSERT INTO attachments (message_id, original_name, size, sha256, state, created_at)"
        " VALUES (?,?,?,?,'uploading',?)",
        (message["id"], name, size, sha256, utcnow()),
    )
    upload_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO upload_sessions (id, attachment_id, station_id, total_size, chunk_size,"
        " received_chunks, state, created_at, updated_at) VALUES (?,?,?,?,?,'[]','active',?,?)",
        (upload_id, attachment_id, station_id, size, cfg.storage.chunk_size, utcnow(), utcnow()),
    )
    return {
        "attachment_id": attachment_id,
        "upload_id": upload_id,
        "chunk_size": cfg.storage.chunk_size,
    }


def send_message(db: Database, station_id: int, message_id: int) -> dict:
    """Перевод draft → sent. Идемпотентен: повтор не создаёт второго сообщения."""
    message = db.one(
        "SELECT * FROM messages WHERE id = ? AND sender_id = ?", (message_id, station_id)
    )
    if message is None:
        raise StorageError("Сообщение не найдено", 404)
    if message["status"] == "sent":
        return {"message_id": message_id, "status": "sent", "already": True}

    pending = db.query(
        "SELECT id, original_name, state FROM attachments"
        " WHERE message_id = ? AND state != 'ready'",
        (message_id,),
    )
    if pending:
        names = [f"{r['original_name']} ({r['state']})" for r in pending]
        raise StorageError(f"Не все вложения готовы: {', '.join(names)}", 409)

    db.execute(
        "UPDATE messages SET status = 'sent', sent_at = ? WHERE id = ?", (utcnow(), message_id)
    )
    for row in db.query(
        "SELECT recipient_id FROM message_recipients WHERE message_id = ?", (message_id,)
    ):
        emit(db, row["recipient_id"], NEW_MESSAGE, message_id)
    audit(db, station_id, "message.send", message_id)
    return {"message_id": message_id, "status": "sent"}


def inbox(db: Database, station_id: int) -> list[dict]:
    rows = db.query(
        "SELECT m.id, m.subject, m.body, m.sent_at, s.display_name AS sender,"
        "       r.is_read, r.downloaded_at"
        " FROM message_recipients r"
        " JOIN messages m ON m.id = r.message_id"
        " JOIN stations s ON s.id = m.sender_id"
        " WHERE r.recipient_id = ? AND m.status = 'sent' AND r.deleted_by_recipient = 0"
        " ORDER BY m.sent_at DESC",
        (station_id,),
    )
    return [_with_attachments(db, dict(r)) for r in rows]


def sent(db: Database, station_id: int) -> list[dict]:
    rows = db.query(
        "SELECT id, subject, body, sent_at, status FROM messages"
        " WHERE sender_id = ? AND deleted_by_sender = 0 ORDER BY created_at DESC",
        (station_id,),
    )
    result = []
    for row in rows:
        item = _with_attachments(db, dict(row))
        item["recipients"] = [
            dict(r)
            for r in db.query(
                "SELECT s.display_name AS name, r.is_read, r.downloaded_at"
                " FROM message_recipients r JOIN stations s ON s.id = r.recipient_id"
                " WHERE r.message_id = ?",
                (row["id"],),
            )
        ]
        result.append(item)
    return result


def get_message(db: Database, station_id: int, message_id: int) -> dict:
    row = db.one(
        "SELECT m.*, s.display_name AS sender FROM messages m"
        " JOIN stations s ON s.id = m.sender_id WHERE m.id = ?",
        (message_id,),
    )
    if row is None or not _has_access(db, station_id, message_id):
        raise StorageError("Сообщение не найдено", 404)
    item = _with_attachments(db, dict(row))
    item["recipients"] = [
        dict(r)
        for r in db.query(
            "SELECT s.display_name AS name, r.is_read, r.downloaded_at"
            " FROM message_recipients r JOIN stations s ON s.id = r.recipient_id"
            " WHERE r.message_id = ?",
            (message_id,),
        )
    ]
    return item


def mark_read(db: Database, station_id: int, message_id: int) -> None:
    row = db.one(
        "SELECT id, is_read FROM message_recipients WHERE message_id = ? AND recipient_id = ?",
        (message_id, station_id),
    )
    if row is None:
        raise StorageError("Сообщение не найдено", 404)
    if row["is_read"]:
        return
    db.execute("UPDATE message_recipients SET is_read = 1 WHERE id = ?", (row["id"],))
    sender_id = db.scalar("SELECT sender_id FROM messages WHERE id = ?", (message_id,))
    emit(db, sender_id, READ, message_id)


def mark_downloaded(db: Database, station_id: int, message_id: int) -> None:
    row = db.one(
        "SELECT id, downloaded_at FROM message_recipients"
        " WHERE message_id = ? AND recipient_id = ?",
        (message_id, station_id),
    )
    if row is None:
        raise StorageError("Сообщение не найдено", 404)
    if row["downloaded_at"]:
        return
    db.execute(
        "UPDATE message_recipients SET downloaded_at = ?, is_read = 1 WHERE id = ?",
        (utcnow(), row["id"]),
    )
    sender_id = db.scalar("SELECT sender_id FROM messages WHERE id = ?", (message_id,))
    emit(db, sender_id, DOWNLOADED, message_id)
    audit(db, station_id, "message.ack", message_id)


def hide_message(db: Database, station_id: int, message_id: int) -> None:
    """Скрыть у себя. Файл на диске остаётся, пока его не скрыли все (2.3)."""
    updated = db.execute(
        "UPDATE message_recipients SET deleted_by_recipient = 1"
        " WHERE message_id = ? AND recipient_id = ?",
        (message_id, station_id),
    ).rowcount
    if not updated:
        updated = db.execute(
            "UPDATE messages SET deleted_by_sender = 1 WHERE id = ? AND sender_id = ?",
            (message_id, station_id),
        ).rowcount
    if not updated:
        raise StorageError("Сообщение не найдено", 404)
    emit(db, station_id, DELETED, message_id)


def find_orphaned(db: Database) -> list[int]:
    """Вложения, которые скрыли у себя все участники. Счётчик ссылок не заводим (2.3)."""
    rows = db.query(
        "SELECT a.id FROM attachments a"
        " JOIN messages m ON m.id = a.message_id"
        " WHERE a.state = 'ready' AND m.deleted_by_sender = 1"
        "   AND NOT EXISTS (SELECT 1 FROM message_recipients r"
        "                   WHERE r.message_id = m.id AND r.deleted_by_recipient = 0)"
    )
    return [r["id"] for r in rows]


def _owned_draft(db: Database, station_id: int, message_id: int):
    message = db.one(
        "SELECT * FROM messages WHERE id = ? AND sender_id = ?", (message_id, station_id)
    )
    if message is None:
        raise StorageError("Сообщение не найдено", 404)
    if message["status"] != "draft":
        raise StorageError("Сообщение уже отправлено", 409)
    return message


def _has_access(db: Database, station_id: int, message_id: int) -> bool:
    if db.one(
        "SELECT 1 FROM messages WHERE id = ? AND sender_id = ?", (message_id, station_id)
    ):
        return True
    return bool(
        db.one(
            "SELECT 1 FROM message_recipients r JOIN messages m ON m.id = r.message_id"
            " WHERE r.message_id = ? AND r.recipient_id = ? AND m.status = 'sent'",
            (message_id, station_id),
        )
    )


def _with_attachments(db: Database, item: dict) -> dict:
    item["attachments"] = [
        dict(r)
        for r in db.query(
            "SELECT id, original_name, size, sha256, state FROM attachments"
            " WHERE message_id = ?",
            (item["id"],),
        )
    ]
    item["total_size"] = sum(a["size"] for a in item["attachments"])
    return item


def attachment_for_download(db: Database, station_id: int, attachment_id: int):
    row = db.one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
    if row is None:
        raise StorageError("Вложение не найдено", 404)
    if not _has_access(db, station_id, row["message_id"]):
        raise StorageError("Вложение не найдено", 404)
    if row["state"] == "missing":
        raise StorageError("Файл отсутствует на сервере, обратитесь к администратору", 410)
    if row["state"] != "ready":
        raise StorageError(f"Вложение ещё не готово (состояние: {row['state']})", 409)
    return row


def parse_recipients(raw) -> list[int]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [int(x) for x in raw]
