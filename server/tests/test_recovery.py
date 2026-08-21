"""Восстановление после сбоя, резервирование места, сверка хранилища. Разделы 2.7, 2.12, 2.13."""

from __future__ import annotations

import hashlib
import json
import os

from filepost import recovery, storage
from filepost.config import Config
from filepost.db import Database
from helpers import wait_ready


def _start_upload(station, recipient_id: int, data: bytes) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    message_id = station.post(
        "/api/messages", json={"subject": "t", "body": "", "recipients": [recipient_id]}
    ).json()["message_id"]
    upload = station.post(
        f"/api/messages/{message_id}/attachments/init",
        json={"name": "f.bin", "size": len(data), "sha256": digest},
    ).json()
    upload["message_id"] = message_id
    return upload


def test_interrupted_assembly_is_finished_on_start(buh, sklad, cfg: Config, db: Database):
    """Сессия закоммичена, вложение осталось uploading — служба досбирает при старте."""
    data = os.urandom(2 * 1024 * 1024)
    upload = _start_upload(buh, sklad.station_id, data)
    chunk_size = upload["chunk_size"]
    for index, start in enumerate(range(0, len(data), chunk_size)):
        buh.put(f"/api/uploads/{upload['upload_id']}/chunk/{index}",
                content=data[start:start + chunk_size])

    # Имитируем падение ровно между «сессия закоммичена» и «вложение готово».
    db.execute("UPDATE upload_sessions SET state = 'committed' WHERE id = ?",
               (upload["upload_id"],))

    report = recovery.run(db, cfg)
    assert upload["attachment_id"] in report.reassembled
    state = db.scalar("SELECT state FROM attachments WHERE id = ?", (upload["attachment_id"],))
    assert state == "ready"


def test_file_moved_but_db_not_updated(buh, sklad, cfg: Config, db: Database):
    """Падение между os.replace и UPDATE: файл на месте, запись отстала."""
    data = os.urandom(4096)
    _, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"

    db.execute("UPDATE attachments SET state = 'uploading' WHERE id = ?", (attachment_id,))
    report = recovery.run(db, cfg)

    assert attachment_id in report.confirmed
    assert db.scalar("SELECT state FROM attachments WHERE id = ?", (attachment_id,)) == "ready"


def test_ready_without_file_marked_missing(buh, sklad, cfg: Config, db: Database):
    data = os.urandom(4096)
    _, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"

    path = storage.resolve(cfg, db.scalar(
        "SELECT storage_path FROM attachments WHERE id = ?", (attachment_id,)))
    path.unlink()

    report = recovery.run(db, cfg)
    assert attachment_id in report.marked_missing
    # Скачивание такого вложения должно давать понятный отказ, а не 500.
    r = sklad.get(f"/api/attachments/{attachment_id}/download")
    assert r.status_code == 410


def test_storage_verify_finds_both_directions(buh, sklad, cfg: Config, db: Database):
    """После восстановления БД из копии расхождение двустороннее (2.13)."""
    data = os.urandom(2048)
    _, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"

    # запись есть, файла нет
    storage.resolve(cfg, db.scalar(
        "SELECT storage_path FROM attachments WHERE id = ?", (attachment_id,))).unlink()
    # файл есть, записи нет
    orphan = cfg.storage_path / "2026" / "08" / "deadbeef.bin"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"x" * 100)

    report = recovery.verify_storage(db, cfg)
    assert [m["attachment_id"] for m in report.missing_files] == [attachment_id]
    assert any("deadbeef" in p for p in report.orphan_files)


def test_reservation_blocks_overcommit(buh, sklad, cfg: Config, db: Database):
    """Резерв не даёт нескольким заливкам одновременно пройти проверку места."""
    free = storage.free_space(cfg.storage_path)
    # Порог ставим так, чтобы поместилась ровно одна заливка половины свободного места.
    cfg.storage.min_free_space_gb = 0
    cfg.storage.max_file_size_gb = 1024
    size = int(free * 0.6)

    first = _start_upload(buh, sklad.station_id, b"")
    db.execute("UPDATE upload_sessions SET total_size = ? WHERE id = ?",
               (size, first["upload_id"]))

    assert storage.reserved_bytes(db) >= size
    r = buh.post(
        f"/api/messages/{first['message_id']}/attachments/init",
        json={"name": "second.bin", "size": size, "sha256": "b" * 64},
    )
    assert r.status_code == 507
    assert "нет места" in r.json()["error"]


def test_reservation_survives_restart(buh, sklad, cfg: Config, db: Database):
    """Резерв вычисляется из БД, поэтому перезапуск его не теряет и не подвешивает."""
    data = os.urandom(1024)
    upload = _start_upload(buh, sklad.station_id, data)
    db.execute("UPDATE upload_sessions SET total_size = ? WHERE id = ?",
               (5_000_000, upload["upload_id"]))

    before = storage.reserved_bytes(db)
    db.close()  # как будто служба перезапустилась
    after = storage.reserved_bytes(db)
    assert before == after == 5_000_000


def test_stale_session_releases_reservation(buh, sklad, cfg: Config, db: Database):
    upload = _start_upload(buh, sklad.station_id, os.urandom(1024))
    db.execute("UPDATE upload_sessions SET total_size = ?, updated_at = ? WHERE id = ?",
               (1_000_000, "2020-01-01T00:00:00Z", upload["upload_id"]))
    assert storage.reserved_bytes(db) == 1_000_000

    storage.release_stale_reservations(db, cfg)
    assert storage.reserved_bytes(db) == 0


def test_parallel_upload_limit(buh, sklad, cfg: Config):
    cfg.limits.max_parallel_uploads_per_user = 1
    _start_upload(buh, sklad.station_id, b"x")
    message_id = buh.post(
        "/api/messages", json={"subject": "t", "body": "", "recipients": [sklad.station_id]}
    ).json()["message_id"]
    r = buh.post(
        f"/api/messages/{message_id}/attachments/init",
        json={"name": "b.bin", "size": 10, "sha256": "c" * 64},
    )
    assert r.status_code == 429


def test_db_backup_is_readable(cfg: Config, db: Database, buh, tmp_path):
    """Копия живой базы в режиме WAL должна открываться и проходить integrity_check."""
    import sqlite3

    target = tmp_path / "backup.db"
    db.backup_to(target)

    conn = sqlite3.connect(target)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0] >= 1
    finally:
        conn.close()


def test_orphaned_found_when_all_hid_it(buh, sklad, db: Database):
    from filepost.messages import find_orphaned

    data = os.urandom(512)
    message_id, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"

    assert find_orphaned(db) == []
    sklad.delete(f"/api/messages/{message_id}")
    assert find_orphaned(db) == [], "отправитель ещё видит сообщение"
    buh.delete(f"/api/messages/{message_id}")
    assert find_orphaned(db) == [attachment_id]
