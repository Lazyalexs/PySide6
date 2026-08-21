"""Планировщик: уборка, обрезка журнала, бэкап, очистка по сроку. Разделы 2.6, 2.13."""

from __future__ import annotations

import os
from pathlib import Path

from filepost import housekeeper as hk
from filepost import storage
from filepost.config import Config
from filepost.db import Database
from helpers import wait_ready

OLD = "2020-01-01T00:00:00Z"


def test_abandoned_upload_is_swept(buh, sklad, cfg: Config, db: Database):
    """Чанки в tmp\\ живут abandoned_uploads_hours и убираются вместе с сессией."""
    message_id = buh.post(
        "/api/messages", json={"subject": "t", "body": "", "recipients": [sklad.station_id]}
    ).json()["message_id"]
    upload = buh.post(
        f"/api/messages/{message_id}/attachments/init",
        json={"name": "f.bin", "size": 1024, "sha256": "a" * 64},
    ).json()
    buh.put(f"/api/uploads/{upload['upload_id']}/chunk/0", content=b"x" * 1024)

    directory = cfg.tmp_path / upload["upload_id"]
    assert directory.exists()

    db.execute("UPDATE upload_sessions SET updated_at = ? WHERE id = ?",
               (OLD, upload["upload_id"]))
    removed = hk.cleanup_abandoned_uploads(db, cfg)

    assert removed == 1
    assert not directory.exists()
    assert db.one("SELECT 1 FROM upload_sessions WHERE id = ?", (upload["upload_id"],)) is None
    # Вложение переходит в failed, а не остаётся вечно uploading.
    assert db.scalar("SELECT state FROM attachments WHERE id = ?",
                     (upload["attachment_id"],)) == "failed"


def test_fresh_upload_is_not_swept(buh, sklad, cfg: Config, db: Database):
    message_id = buh.post(
        "/api/messages", json={"subject": "t", "body": "", "recipients": [sklad.station_id]}
    ).json()["message_id"]
    upload = buh.post(
        f"/api/messages/{message_id}/attachments/init",
        json={"name": "f.bin", "size": 1024, "sha256": "a" * 64},
    ).json()
    buh.put(f"/api/uploads/{upload['upload_id']}/chunk/0", content=b"x" * 1024)

    assert hk.cleanup_abandoned_uploads(db, cfg) == 0
    assert (cfg.tmp_path / upload["upload_id"]).exists()


def test_orphan_tmp_directory_removed(cfg: Config, db: Database):
    """Каталог без сессии — например, остался после восстановления БД из копии."""
    stray = cfg.tmp_path / "no-such-session"
    stray.mkdir(parents=True)
    (stray / "chunk_0000").write_bytes(b"x")
    os.utime(stray, (0, 0))

    assert hk.cleanup_abandoned_uploads(db, cfg) == 1
    assert not stray.exists()


def test_events_trimmed_by_age(buh, sklad, db: Database, cfg: Config):
    buh.send_file([sklad.station_id], os.urandom(64))
    assert db.scalar("SELECT COUNT(*) FROM events") > 0

    db.execute("UPDATE events SET created_at = ?", (OLD,))
    trimmed = hk.trim_events(db, cfg)

    assert trimmed > 0
    assert db.scalar("SELECT COUNT(*) FROM events") == 0


def test_recent_events_survive(buh, sklad, db: Database, cfg: Config):
    buh.send_file([sklad.station_id], os.urandom(64))
    before = db.scalar("SELECT COUNT(*) FROM events")
    assert hk.trim_events(db, cfg) == 0
    assert db.scalar("SELECT COUNT(*) FROM events") == before


def test_orphaned_marked_but_not_deleted(buh, sklad, cfg: Config, db: Database):
    """delete_orphaned выключен: помечаем, но не удаляем — решение за администратором."""
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"
    path = storage.resolve(cfg, db.scalar(
        "SELECT storage_path FROM attachments WHERE id = ?", (attachment_id,)))

    sklad.delete(f"/api/messages/{message_id}")
    buh.delete(f"/api/messages/{message_id}")

    report = hk.sweep(db, cfg)
    assert attachment_id in report.orphaned_marked
    assert db.scalar("SELECT state FROM attachments WHERE id = ?",
                     (attachment_id,)) == "orphaned"
    assert path.exists(), "файл на диске остаётся, пока администратор не решит иначе"


def test_delete_orphaned_when_enabled(buh, sklad, cfg: Config, db: Database):
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"
    path = storage.resolve(cfg, db.scalar(
        "SELECT storage_path FROM attachments WHERE id = ?", (attachment_id,)))

    sklad.delete(f"/api/messages/{message_id}")
    buh.delete(f"/api/messages/{message_id}")
    cfg.retention.delete_orphaned = True

    hk.sweep(db, cfg)
    assert not path.exists()
    # Строка остаётся: история переписки и аудит целы.
    assert db.scalar("SELECT state FROM attachments WHERE id = ?",
                     (attachment_id,)) == "deleted"
    assert buh.get(f"/api/messages/{message_id}").status_code == 200


def test_retention_disabled_by_default(buh, sklad, cfg: Config, db: Database):
    """ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО — ничего не удаляется само."""
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(128))
    assert wait_ready(buh, attachment_id) == "ready"
    db.execute("UPDATE messages SET sent_at = ? WHERE id = ?", (OLD, message_id))

    deleted, warned = hk.apply_retention(db, cfg)
    assert deleted == [] and warned == []
    assert db.scalar("SELECT state FROM attachments WHERE id = ?",
                     (attachment_id,)) == "ready"


def test_retention_deletes_when_enabled(buh, sklad, cfg: Config, db: Database):
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(128))
    assert wait_ready(buh, attachment_id) == "ready"
    db.execute("UPDATE messages SET sent_at = ? WHERE id = ?", (OLD, message_id))
    cfg.retention.enabled = True

    deleted, _ = hk.apply_retention(db, cfg)
    assert attachment_id in deleted
    assert db.scalar("SELECT state FROM attachments WHERE id = ?",
                     (attachment_id,)) == "deleted"


def test_backup_created_and_rotated(cfg: Config, db: Database, buh, tmp_path: Path):
    cfg.backup.path = str(tmp_path / "backups")
    cfg.backup.keep_copies = 2
    Path(cfg.backup.path).mkdir(parents=True)

    # Старые копии, которые должны выпасть при ротации.
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (Path(cfg.backup.path) / f"filepost-{day}.db").write_bytes(b"old")

    path = hk.daily_backup(db, cfg, force=True)
    assert path is not None

    copies = sorted(Path(cfg.backup.path).glob("filepost-*.db"))
    assert len(copies) == 2, "лишние копии ротируются"
    assert Path(path).exists()


def test_backup_not_repeated_same_day(cfg: Config, db: Database, tmp_path: Path):
    """Расписание: копия делается один раз в сутки."""
    cfg.backup.path = str(tmp_path / "backups")
    cfg.backup.time = "00:00"  # время уже наступило
    assert hk.daily_backup(db, cfg) is not None
    assert hk.daily_backup(db, cfg) is None, "копия за сегодня уже есть"
    # Ручной запуск перезаписывает сегодняшнюю копию — это «сделать сейчас».
    assert hk.daily_backup(db, cfg, force=True) is not None


def test_backup_waits_for_scheduled_time(cfg: Config, db: Database, tmp_path: Path):
    cfg.backup.path = str(tmp_path / "backups")
    cfg.backup.time = "23:59"
    assert hk.daily_backup(db, cfg) is None


def test_backup_disabled(cfg: Config, db: Database, tmp_path: Path):
    cfg.backup.enabled = False
    cfg.backup.path = str(tmp_path / "backups")
    assert hk.daily_backup(db, cfg, force=True) is None


def test_low_space_warning(cfg: Config, db: Database):
    cfg.storage.min_free_space_gb = 10_000  # заведомо больше, чем есть
    assert hk.check_space(db, cfg) is True
    actions = {r["action"] for r in db.query("SELECT action FROM audit_log")}
    assert "storage.low_space" in actions


def test_manual_sweep_endpoint(buh, sklad):
    r = buh.post("/api/admin/housekeeping")
    assert r.status_code == 200
    assert "events_trimmed" in r.json()
    assert sklad.post("/api/admin/housekeeping").status_code == 403


def test_old_client_version_rejected(client, cfg: Config, db: Database):
    """Обновили сервер — семь .exe старой версии в сети (2.6)."""
    from filepost.auth import create_enrollment_code

    code = create_enrollment_code(db, cfg)["enrollment_code"]
    r = client.post(
        "/api/stations/register",
        json={"enrollment_code": code, "display_name": "Старая", "client_version": "0.9.0"},
    )
    data = r.json()
    cfg.server.min_client_version = "1.0.0"

    r = client.post(
        "/api/auth/token",
        json={"station_id": data["station_id"], "secret": data["secret"],
              "client_version": "0.9.0"},
    )
    assert r.status_code == 426
    assert "Обновите программу" in r.json()["error"]


def test_housekeeper_thread_starts_and_stops(cfg: Config, db: Database):
    keeper = hk.Housekeeper(db, cfg, interval=1)
    keeper.start()
    keeper.stop()
    assert keeper._thread is None


def test_discovery_responder_answers(cfg: Config):
    """Ответчик проверяется юникастом: broadcast не заворачивается внутри контейнера,
    но путь запрос-ответ тот же."""
    import time

    from filepost.discovery import DiscoveryResponder, discover

    cfg.server.discovery_enabled = True
    cfg.server.discovery_port = 8198
    cfg.server.port = 8080

    responder = DiscoveryResponder(cfg)
    responder.start()
    try:
        time.sleep(0.3)
        url = discover(port=8198, timeout=2.0, targets=["127.0.0.1"])
        assert url == "http://127.0.0.1:8080"
    finally:
        responder.stop()


def test_discovery_disabled_by_default(cfg: Config):
    from filepost.discovery import DiscoveryResponder

    responder = DiscoveryResponder(cfg)
    responder.start()
    assert responder._thread is None, "по умолчанию автопоиск выключен"
