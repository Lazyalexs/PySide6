"""Ядро клиента: регистрация, передача, докачка, синхронизация, работа офлайн."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from conftest import wait_for
from filepost_client.settings import Settings


def make_file(path: Path, size: int) -> tuple[Path, str]:
    data = os.urandom(size)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def wait_transfer(core, transfer_id: int, state: str = "done", timeout: float = 60.0):
    return wait_for(
        lambda: (t := core.store.transfer(transfer_id)) and t["state"] == state, timeout
    )


def test_registration_stores_key_and_no_login_form(server, tmp_path: Path):
    """Формы входа нет: клиент авторизуется ключом станции сам (2.10)."""
    settings = Settings(root=tmp_path / "c1")
    settings.server.url = server.url
    from filepost_client.core import Core

    core = Core(settings)
    core.register(server.enrollment_code(), "Бухгалтерия")

    assert settings.registered
    assert settings.station.secret, "ключ станции сохранён в config.ini"
    assert settings.config_path.exists()

    # Второй запуск: ключ уже есть, вход не требуется.
    reloaded = Settings(root=tmp_path / "c1").load()
    assert reloaded.station.station_id == settings.station.station_id
    core2 = Core(reloaded)
    assert core2.connect() is True
    core.stop()
    core2.stop()


def test_directory_has_no_technical_names(buh, sklad):
    """В интерфейс уходят только человеческие имена: ни IP, ни имени ПК (3.2)."""
    buh.sync()
    stations = buh.stations()
    assert [s["display_name"] for s in stations] == ["Склад"]
    assert all("last_ip" not in s for s in stations)


def test_send_and_receive(buh, sklad, tmp_path: Path):
    path, digest = make_file(tmp_path / "src" / "акты.zip", 2 * 1024 * 1024 + 77)
    buh.transfers.start()
    message_id = buh.compose([sklad.settings.station.station_id], "Акты", "до пятницы", [path])

    transfer = buh.store.transfers()[0]
    assert wait_transfer(buh, transfer["id"]), buh.store.transfer(transfer["id"])
    assert wait_for(lambda: buh.api.message(message_id)["status"] == "sent")

    sklad.transfers.start()
    assert wait_for(lambda: sklad.sync() or sklad.inbox())
    inbox = sklad.inbox()
    assert inbox[0]["subject"] == "Акты"
    assert inbox[0]["sender"] == "Бухгалтерия, окно 2"

    ids = sklad.download_all(inbox[0]["id"])
    assert ids
    assert wait_transfer(sklad, ids[0])

    saved = Path(sklad.store.transfer(ids[0])["file_path"])
    assert saved.exists()
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == digest
    assert saved.parent == sklad.settings.downloads_dir


def test_download_resumes_from_partial(buh, sklad, tmp_path: Path):
    """Незавершённый .part продолжается с нужного байта через Range (3.4)."""
    path, digest = make_file(tmp_path / "src" / "big.bin", 1024 * 1024)
    buh.transfers.start()
    message_id = buh.compose([sklad.settings.station.station_id], "t", "", [path])
    assert wait_transfer(buh, buh.store.transfers()[0]["id"])

    assert wait_for(lambda: sklad.sync() or sklad.inbox())
    item = sklad.inbox()[0]
    attachment = item["attachments"][0]

    # Кладём половину файла как .part — будто предыдущая попытка оборвалась.
    sklad.settings.partial_dir.mkdir(parents=True, exist_ok=True)
    partial = sklad.settings.partial_dir / f"{attachment['id']}_{attachment['original_name']}.part"
    original = path.read_bytes()
    partial.write_bytes(original[: len(original) // 2])

    sklad.transfers.start()
    ids = sklad.download_all(item["id"])
    assert wait_transfer(sklad, ids[0])

    saved = Path(sklad.store.transfer(ids[0])["file_path"])
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == digest


def test_upload_queue_survives_restart(buh, sklad, tmp_path: Path):
    """Состояние очереди в client.db: перезапуск продолжает заливку (3.4)."""
    path, _ = make_file(tmp_path / "src" / "resume.bin", 3 * 1024 * 1024)
    message_id = buh.compose([sklad.settings.station.station_id], "t", "", [path])
    transfer_id = buh.store.transfers()[0]["id"]

    # Приложение закрыли до старта передач.
    assert buh.store.transfer(transfer_id)["state"] == "queued"

    from filepost_client.core import Core

    reopened = Core(Settings(root=buh.settings.root).load())
    assert reopened.connect()
    assert reopened.transfers.resume_pending() == 1
    reopened.transfers.start()
    assert wait_transfer(reopened, transfer_id)
    reopened.stop()


def test_cursor_saved_only_after_applying(buh, sklad, tmp_path: Path):
    """Курсор двигается после применения событий, а не до (3.5)."""
    assert sklad.store.last_event_id == 0
    path, _ = make_file(tmp_path / "src" / "a.bin", 1024)
    buh.transfers.start()
    buh.compose([sklad.settings.station.station_id], "письмо", "", [path])
    assert wait_transfer(buh, buh.store.transfers()[0]["id"])

    assert wait_for(lambda: sklad.sync())
    assert sklad.store.last_event_id > 0
    assert sklad.inbox(), "сообщение применено до сохранения курсора"


def test_resync_when_server_db_rolled_back(buh, sklad, server, tmp_path: Path):
    """Курсор впереди журнала — сервер требует пересинхронизации (2.13)."""
    path, _ = make_file(tmp_path / "src" / "b.bin", 512)
    buh.transfers.start()
    buh.compose([sklad.settings.station.station_id], "первое", "", [path])
    assert wait_transfer(buh, buh.store.transfers()[0]["id"])
    assert wait_for(lambda: sklad.sync())
    assert sklad.inbox()

    # Имитируем откат БД сервера: курсор станции стал недостижимым.
    sklad.store.set_last_event_id(sklad.store.last_event_id + 1000)
    sklad.sync()

    # Клиент перечитал входящие целиком и взял свежий курсор.
    assert sklad.inbox()
    assert sklad.store.last_event_id < 1000


def test_works_offline_from_local_db(buh, sklad, server, tmp_path: Path):
    """Окно открывается мгновенно и что-то показывает при недоступном сервере (3.6)."""
    path, _ = make_file(tmp_path / "src" / "c.bin", 256)
    buh.transfers.start()
    buh.compose([sklad.settings.station.station_id], "офлайн-тест", "", [path])
    assert wait_transfer(buh, buh.store.transfers()[0]["id"])
    assert wait_for(lambda: sklad.sync())
    assert sklad.inbox()

    server.stop()

    # Список читается из локальной БД, а не с сервера.
    assert sklad.inbox()[0]["subject"] == "офлайн-тест"
    assert sklad.stations(), "адресная книга тоже закэширована"

    sklad.sync()
    assert sklad.online is False
    assert sklad.last_error == "Нет связи с сервером"


def test_outbox_waits_for_connection(buh, sklad, server, tmp_path: Path):
    """Исходящие копятся в очереди и уходят сами, когда связь вернётся (3.6)."""
    path, _ = make_file(tmp_path / "src" / "d.bin", 1024)
    message_id = buh.compose([sklad.settings.station.station_id], "ждёт", "", [path])
    transfer_id = buh.store.transfers()[0]["id"]

    server.stop()
    buh.transfers.start()
    # Без связи задание возвращается в очередь, а не падает в ошибку.
    assert wait_for(
        lambda: buh.store.transfer(transfer_id)["state"] in ("queued", "active"), timeout=10
    )
    assert buh.store.transfer(transfer_id)["state"] != "error"


def test_no_space_gives_clear_message(buh, sklad, server, tmp_path: Path):
    """507 — не ошибка сети: задание встаёт с понятным текстом (3.4)."""
    server.cfg.storage.min_free_space_gb = 10_000  # места заведомо нет
    path, _ = make_file(tmp_path / "src" / "e.bin", 4096)
    buh.compose([sklad.settings.station.station_id], "t", "", [path])
    transfer_id = buh.store.transfers()[0]["id"]

    buh.transfers.start()
    assert wait_transfer(buh, transfer_id, state="error", timeout=30)
    error = buh.store.transfer(transfer_id)["error"]
    assert "нет места" in error.lower()
    assert "администратору" in error


def test_rename_visible_to_others(buh, sklad):
    buh.rename_station("Бухгалтерия, главный")
    assert wait_for(lambda: sklad.sync() is not None)
    sklad._refresh_presence()
    assert any(s["display_name"] == "Бухгалтерия, главный" for s in sklad.stations())


def test_unread_count(buh, sklad, tmp_path: Path):
    path, _ = make_file(tmp_path / "src" / "f.bin", 128)
    buh.transfers.start()
    buh.compose([sklad.settings.station.station_id], "непрочитанное", "", [path])
    assert wait_transfer(buh, buh.store.transfers()[0]["id"])
    assert wait_for(lambda: sklad.sync())

    assert sklad.unread_count() == 1
    sklad.mark_read(sklad.inbox()[0]["id"])
    assert sklad.unread_count() == 0


def test_hide_message(buh, sklad, tmp_path: Path):
    path, _ = make_file(tmp_path / "src" / "g.bin", 128)
    buh.transfers.start()
    buh.compose([sklad.settings.station.station_id], "скрыть", "", [path])
    assert wait_transfer(buh, buh.store.transfers()[0]["id"])
    assert wait_for(lambda: sklad.sync())

    message_id = sklad.inbox()[0]["id"]
    sklad.hide(message_id)
    assert sklad.inbox() == []


def test_settings_roundtrip(tmp_path: Path):
    settings = Settings(root=tmp_path / "s")
    settings.server.url = "http://filepost-srv:8080"
    settings.prefs.auto_download = True
    settings.prefs.parallel_transfers = 1
    settings.prefs.upload_limit_mbps = 50
    settings.save()

    loaded = Settings(root=tmp_path / "s").load()
    assert loaded.server.url == "http://filepost-srv:8080"
    assert loaded.prefs.auto_download is True
    assert loaded.prefs.parallel_transfers == 1
    assert loaded.prefs.upload_limit_mbps == 50


def test_candidate_urls_order(tmp_path: Path):
    settings = Settings(root=tmp_path / "s2")
    settings.server.url = "filepost-srv:8080"
    settings.server.fallback = "10.10.10.5:8080"
    assert settings.candidate_urls() == [
        "http://filepost-srv:8080",
        "http://10.10.10.5:8080",
    ]


def test_fallback_used_when_primary_dead(server, tmp_path: Path):
    from filepost_client.core import Core

    settings = Settings(root=tmp_path / "fb")
    settings.server.url = "http://127.0.0.1:1"  # заведомо мёртвый
    settings.server.fallback = server.url
    core = Core(settings)
    assert core.resolve_server() == server.url
    core.stop()


def test_admin_flag_from_server(buh, sklad):
    assert buh.settings.station.is_admin is True
    assert sklad.settings.station.is_admin is False
    assert buh.api.admin_stations()
