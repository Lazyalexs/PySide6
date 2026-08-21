"""Ограничение параллельных скачиваний, отзыв письма, валидация, деактивация.

Закрывает пункты, которые раньше были объявлены в конфиге, но не работали.
"""

from __future__ import annotations

import os
import threading
import time

from filepost import housekeeper as hk
from filepost.config import Config
from filepost.db import Database
from filepost.storage import DownloadSlots
from helpers import wait_ready

OLD = "2020-01-01T00:00:00Z"


# --------------------------------------------------------------------------- слоты


def test_slots_respect_limit():
    slots = DownloadSlots()
    assert slots.acquire(1, limit=2)
    assert slots.acquire(1, limit=2)
    assert not slots.acquire(1, limit=2), "третье скачивание не должно пройти"
    assert slots.acquire(2, limit=2), "у другой станции свой счёт"

    slots.release(1)
    assert slots.acquire(1, limit=2)


def test_slots_zero_limit_means_unlimited():
    slots = DownloadSlots()
    for _ in range(10):
        assert slots.acquire(1, limit=0)


def test_slots_release_below_zero_is_safe():
    slots = DownloadSlots()
    slots.release(99)
    assert slots.count(99) == 0


def test_slots_are_thread_safe():
    slots = DownloadSlots()
    granted: list[bool] = []
    lock = threading.Lock()

    def worker():
        ok = slots.acquire(1, limit=5)
        with lock:
            granted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 5, "лимит должен соблюдаться при гонке"


def test_parallel_download_limit_enforced(buh, sklad, cfg: Config):
    """Пятеро получателей одного письма не должны разом положить диск (2.6)."""
    cfg.limits.max_parallel_downloads_per_user = 1
    data = os.urandom(512 * 1024)
    _, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"

    slots = sklad.http.app.state.download_slots
    assert slots.acquire(sklad.station_id, 1), "занимаем единственный слот вручную"
    try:
        r = sklad.get(f"/api/attachments/{attachment_id}/download")
        assert r.status_code == 429
        assert "не больше 1" in r.json()["error"]
    finally:
        slots.release(sklad.station_id)

    # Слот освободился — скачивание проходит.
    r = sklad.get(f"/api/attachments/{attachment_id}/download")
    assert r.status_code == 200
    assert r.content == data


def test_slot_released_after_download(buh, sklad, cfg: Config):
    cfg.limits.max_parallel_downloads_per_user = 1
    data = os.urandom(4096)
    _, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"

    slots = sklad.http.app.state.download_slots
    for _ in range(3):
        assert sklad.get(f"/api/attachments/{attachment_id}/download").status_code == 200
    assert slots.count(sklad.station_id) == 0, "слот освобождается после каждой отдачи"


# --------------------------------------------------------------------------- отзыв


def test_revoke_before_download(buh, sklad):
    data = os.urandom(2048)
    message_id, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"
    assert len(sklad.get("/api/inbox").json()) == 1

    r = buh.post(f"/api/messages/{message_id}/revoke")
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"

    # У получателя письмо исчезло, скачать вложение нельзя.
    assert sklad.get("/api/inbox").json() == []
    assert sklad.get(f"/api/attachments/{attachment_id}/download").status_code == 404


def test_revoke_after_download_refused(buh, sklad):
    """Файл уже на чужой машине — делать вид, что мы его вернули, нельзя."""
    data = os.urandom(1024)
    message_id, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"
    sklad.post(f"/api/messages/{message_id}/ack")

    r = buh.post(f"/api/messages/{message_id}/revoke")
    assert r.status_code == 409
    assert "Уже скачано" in r.json()["error"]
    assert "Склад" in r.json()["error"], "должно быть видно, кто именно забрал"


def test_revoke_is_idempotent(buh, sklad):
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"
    buh.post(f"/api/messages/{message_id}/revoke")
    r = buh.post(f"/api/messages/{message_id}/revoke")
    assert r.status_code == 200 and r.json()["already"] is True


def test_only_sender_can_revoke(buh, sklad):
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"
    assert sklad.post(f"/api/messages/{message_id}/revoke").status_code == 404


def test_revoke_emits_event(buh, sklad, db: Database):
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"
    cursor = sklad.get("/api/events?since=0").json()["last_event_id"]

    buh.post(f"/api/messages/{message_id}/revoke")
    events = sklad.get(f"/api/events?since={cursor}").json()["events"]
    assert any(e["type"] == "revoked" for e in events)


def test_revoked_attachment_becomes_orphaned(buh, sklad, db: Database):
    """Получатели отозванное не видят и скрыть не могут — файл иначе завис бы навсегда."""
    from filepost.messages import find_orphaned

    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"
    buh.post(f"/api/messages/{message_id}/revoke")

    assert find_orphaned(db) == [], "отправитель ещё видит письмо в «Отправленных»"
    buh.delete(f"/api/messages/{message_id}")
    assert find_orphaned(db) == [attachment_id]


# --------------------------------------------------------------------------- валидация


def test_subject_length_limited(buh, sklad, cfg: Config):
    cfg.limits.max_subject_length = 10
    r = buh.post(
        "/api/messages",
        json={"subject": "x" * 11, "body": "", "recipients": [sklad.station_id]},
    )
    assert r.status_code == 400
    assert "Тема длиннее" in r.json()["error"]


def test_body_length_limited(buh, sklad, cfg: Config):
    cfg.limits.max_body_length = 5
    r = buh.post(
        "/api/messages",
        json={"subject": "t", "body": "y" * 6, "recipients": [sklad.station_id]},
    )
    assert r.status_code == 400
    assert "Комментарий длиннее" in r.json()["error"]


def test_limits_are_configurable_at_runtime(buh, sklad, cfg: Config):
    """Границы живут в конфиге, а не в схеме запроса: меняются без пересборки."""
    cfg.limits.max_subject_length = 3
    assert buh.post(
        "/api/messages", json={"subject": "abcd", "body": "", "recipients": [sklad.station_id]}
    ).status_code == 400
    cfg.limits.max_subject_length = 200
    assert buh.post(
        "/api/messages", json={"subject": "abcd", "body": "", "recipients": [sklad.station_id]}
    ).status_code == 200


# --------------------------------------------------------------------------- деактивация


def test_deactivation_reports_undelivered(buh, sklad):
    """Администратор должен узнать о непрочитанных сразу, а не по звонку через неделю."""
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(512))
    assert wait_ready(buh, attachment_id) == "ready"

    r = buh.patch(f"/api/admin/stations/{sklad.station_id}", json={"is_active": False})
    assert r.status_code == 200
    body = r.json()
    assert len(body["undelivered"]) == 1
    assert body["undelivered"][0]["message_id"] == message_id
    assert "неполученных писем: 1" in body["warning"]


def test_deactivation_without_pending_has_no_warning(buh, sklad):
    r = buh.patch(f"/api/admin/stations/{sklad.station_id}", json={"is_active": False})
    assert r.json()["undelivered"] == []
    assert "warning" not in r.json()


def test_sender_sees_recipient_deactivated(buh, sklad):
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"
    buh.patch(f"/api/admin/stations/{sklad.station_id}", json={"is_active": False})

    recipients = buh.get("/api/sent").json()[0]["recipients"]
    assert recipients[0]["is_active"] == 0, "отправителю видно, что станция отключена"


# --------------------------------------------------------------------------- retention


def test_retention_warns_sender_before_deleting(buh, sklad, cfg: Config, db: Database):
    """notify_sender_before_days раньше был объявлен, но не работал."""
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"

    cfg.retention.enabled = True
    cfg.retention.delete_never_downloaded_days = 30
    cfg.retention.notify_sender_before_days = 40  # окно предупреждения уже наступило
    db.execute(
        "UPDATE messages SET sent_at = ? WHERE id = ?",
        ("2026-08-01T00:00:00Z", message_id),
    )

    deleted, warned = hk.apply_retention(db, cfg)
    assert deleted == [], "удалять ещё рано"
    assert message_id in warned

    events = buh.get("/api/events?since=0").json()["events"]
    assert any(e["type"] == "retention_warning" for e in events)


def test_retention_warns_only_once(buh, sklad, cfg: Config, db: Database):
    """Повторное событие на каждый прогон уборки превратило бы это в шум."""
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"

    cfg.retention.enabled = True
    cfg.retention.delete_never_downloaded_days = 30
    cfg.retention.notify_sender_before_days = 40
    db.execute(
        "UPDATE messages SET sent_at = ? WHERE id = ?",
        ("2026-08-01T00:00:00Z", message_id),
    )

    assert hk.apply_retention(db, cfg)[1] == [message_id]
    assert hk.apply_retention(db, cfg)[1] == [], "второй раз не предупреждаем"


def test_no_warning_when_retention_disabled(buh, sklad, cfg: Config, db: Database):
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"
    db.execute("UPDATE messages SET sent_at = ? WHERE id = ?", (OLD, message_id))
    assert hk.apply_retention(db, cfg) == ([], [])


# --------------------------------------------------------------------------- упаковка


def test_init_writes_ascii_code_file(tmp_path, monkeypatch):
    """Установщик читает код из файла, а не из вывода консоли.

    В выводе русский текст в кодировке консоли — в мастере он превратился бы
    в кракозябры. Сам код чистый ASCII, поэтому читается однозначно.
    """
    from filepost.cli import main

    config = tmp_path / "config.toml"
    config.write_text(
        '[storage]\npath = "storage"\ntmp_path = "tmp"\nmin_free_space_gb = 0\n'
        "[backup]\nenabled = false\n",
        encoding="utf-8",
    )
    assert main(["--config", str(config), "init"]) == 0

    code_file = tmp_path / "logs" / "enrollment-code.txt"
    assert code_file.exists(), "установщику нечего будет показать администратору"

    code = code_file.read_text(encoding="ascii").strip()
    assert len(code) == 14 and code.count("-") == 2, code
    assert code.replace("-", "").isalnum() and code.isupper()


def test_init_refuses_second_run(tmp_path):
    """Повторный init не должен затирать существующую базу."""
    from filepost.cli import main

    config = tmp_path / "config.toml"
    config.write_text(
        '[storage]\npath = "storage"\ntmp_path = "tmp"\nmin_free_space_gb = 0\n'
        "[backup]\nenabled = false\n",
        encoding="utf-8",
    )
    assert main(["--config", str(config), "init"]) == 0
    assert main(["--config", str(config), "init"]) == 1
