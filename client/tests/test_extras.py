"""Троттлинг отдачи, звук уведомлений, локальные черновики."""

from __future__ import annotations

import os
import sys
import time
import wave
from pathlib import Path

import pytest

from conftest import wait_for
from filepost_client import sound
from filepost_client.transfers import MB, RateLimiter


# --------------------------------------------------------------------------- скорость


def test_limiter_disabled_by_default():
    limiter = RateLimiter(0)
    assert not limiter.enabled
    assert limiter.account(50 * MB) == 0.0, "без предела не ждём никогда"


def test_limiter_delays_when_ahead_of_limit():
    """Отдали мегабайт при пределе 1 МБ/с — следующий должен подождать около секунды."""
    limiter = RateLimiter(1)
    started = time.monotonic()
    limiter.account(MB)
    elapsed = time.monotonic() - started
    assert 0.7 <= elapsed <= 1.6, f"ожидали паузу около секунды, вышло {elapsed:.2f}"


def test_limiter_does_not_delay_when_behind():
    """Если реальная скорость и так ниже предела, тормозить нечего."""
    limiter = RateLimiter(100)
    time.sleep(0.15)
    assert limiter.account(MB) == 0.0


def test_limiter_average_converges_to_limit():
    limiter = RateLimiter(4)  # 4 МБ/с
    started = time.monotonic()
    for _ in range(4):
        limiter.account(MB)
    elapsed = time.monotonic() - started
    # 4 МБ при 4 МБ/с — примерно секунда, с поправкой на дискретность сна.
    assert 0.7 <= elapsed <= 1.5, f"вышло {elapsed:.2f} с"


def test_limiter_stops_waiting_on_pause():
    """Пауза и отмена срабатывают сразу, а не через минуту ожидания."""
    limiter = RateLimiter(1)
    started = time.monotonic()
    limiter.account(20 * MB, should_stop=lambda: True)
    assert time.monotonic() - started < 1.0


def test_limiter_reset():
    """После сброса накопленный долг обнуляется: килобайт не ждёт секунду."""
    limiter = RateLimiter(1)
    limiter.account(MB)
    limiter.reset()
    assert limiter.account(1024) < 0.05


def test_upload_limit_reaches_transfer_manager(buh):
    buh.settings.prefs.upload_limit_mbps = 7
    buh.apply_settings()
    assert buh.transfers.limiter.limit == 7 * MB

    buh.settings.prefs.upload_limit_mbps = 0
    buh.apply_settings()
    assert not buh.transfers.limiter.enabled


def test_throttled_upload_still_delivers(buh, sklad, tmp_path: Path):
    """Ограничение замедляет отдачу, но файл доходит целиком."""
    path = tmp_path / "slow.bin"
    path.write_bytes(os.urandom(2 * MB))

    buh.settings.prefs.upload_limit_mbps = 8
    buh.apply_settings()
    buh.transfers.start()
    buh.compose([sklad.settings.station.station_id], "медленно", "", [path])

    transfer_id = buh.store.transfers()[0]["id"]
    assert wait_for(
        lambda: buh.store.transfer(transfer_id)["state"] == "done", timeout=60
    ), buh.store.transfer(transfer_id)
    assert buh.store.transfer(transfer_id)["transferred"] == 2 * MB


# --------------------------------------------------------------------------- звук


def test_sound_resource_exists():
    path = sound.resource_path()
    assert path.exists(), f"звук уведомления не найден: {path}"
    assert path.suffix == ".wav", "winsound умеет только WAV"


def test_sound_is_valid_wav():
    with wave.open(str(sound.resource_path()), "rb") as handle:
        assert handle.getnchannels() in (1, 2)
        assert handle.getframerate() >= 8000
        assert handle.getnframes() > 0


def test_sound_is_short_enough_for_a_notification():
    """Сигнал длиннее пары секунд на каждом письме раздражает и его выключают.

    Страховка на случай пересборки WAV из исходника без обрезки: исходник длится
    3,2 с, из них почти две — тишина.
    """
    with wave.open(str(sound.resource_path()), "rb") as handle:
        seconds = handle.getnframes() / handle.getframerate()
    assert 0.2 <= seconds <= 2.0, f"длительность {seconds:.2f} с вне разумного для уведомления"


def test_sound_fades_out_without_click():
    """Обрезка без затухания даёт щелчок на конце.

    Меряем самую границу — последнюю миллисекунду, а не длинное окно: щелчок
    возникает от разрыва на стыке, а не от того, что сигнал вообще ещё звучит
    за десяток миллисекунд до конца. Порог берём от полной шкалы, потому что
    слышимость разрыва зависит от абсолютного уровня, а не от пика этого клипа.
    """
    import struct

    with wave.open(str(sound.resource_path()), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.getnframes()
        samples = struct.unpack(f"<{frames}h", handle.readframes(frames))

    boundary = max(1, rate // 1000)  # последняя миллисекунда
    tail = max(abs(x) for x in samples[-boundary:])
    assert tail < 0.01 * 32767, f"на срезе амплитуда {tail} — будет слышен щелчок"


def test_sound_disabled_does_nothing():
    assert sound.play_notification(enabled=False) is False


def test_sound_never_raises_on_missing_file(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sound, "resource_path", lambda name=".": tmp_path / "нет.wav")
    assert sound.play_notification(True) is False


@pytest.mark.skipif(sys.platform == "win32", reason="проверяем поведение вне Windows")
def test_sound_is_noop_outside_windows():
    """Вне Windows звук не играется, но и не падает: это среда разработки."""
    assert sound.play_notification(True) is False
    assert sound.available() is False


# --------------------------------------------------------------------------- черновики


def test_draft_saved_locally_without_server(buh, sklad, server, tmp_path: Path):
    """Черновик живёт локально: серверу до отправки о нём ничего не известно."""
    path = tmp_path / "draft.bin"
    path.write_bytes(b"x" * 100)
    server.stop()

    draft_id = buh.save_draft(
        [sklad.settings.station.station_id], "не дописал", "текст", [path]
    )
    assert draft_id
    drafts = buh.drafts()
    assert len(drafts) == 1
    assert drafts[0]["subject"] == "не дописал"
    assert drafts[0]["files"] == [str(path)]
    assert drafts[0]["recipients"] == [sklad.settings.station.station_id]


def test_draft_survives_restart(buh, sklad, tmp_path: Path):
    path = tmp_path / "d2.bin"
    path.write_bytes(b"y" * 50)
    buh.save_draft([sklad.settings.station.station_id], "переживёт", "", [path])

    from filepost_client.core import Core
    from filepost_client.settings import Settings

    reopened = Core(Settings(root=buh.settings.root).load())
    try:
        assert [d["subject"] for d in reopened.drafts()] == ["переживёт"]
    finally:
        reopened.stop()


def test_draft_updated_not_duplicated(buh, sklad):
    draft_id = buh.save_draft([], "первая версия", "", [])
    same = buh.save_draft([], "вторая версия", "", [], draft_id=draft_id)
    assert same == draft_id
    assert len(buh.drafts()) == 1
    assert buh.drafts()[0]["subject"] == "вторая версия"


def test_draft_removed_after_send(buh, sklad, tmp_path: Path):
    path = tmp_path / "d3.bin"
    path.write_bytes(os.urandom(1024))
    draft_id = buh.save_draft([sklad.settings.station.station_id], "уйдёт", "", [path])
    assert buh.drafts()

    buh.transfers.start()
    buh.compose(
        [sklad.settings.station.station_id], "уйдёт", "", [path], draft_id=draft_id
    )
    assert buh.drafts() == [], "черновик убирается после отправки"


def test_delete_draft(buh):
    draft_id = buh.save_draft([], "на удаление", "", [])
    buh.delete_draft(draft_id)
    assert buh.drafts() == []


def test_drafts_folder_in_ui(buh, sklad, tmp_path: Path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from filepost_client.ui.main_window import DRAFTS, MainWindow

    app = QApplication.instance() or QApplication([])
    path = tmp_path / "ui-draft.bin"
    path.write_bytes(b"z" * 10)
    buh._refresh_presence()  # адресная книга: sklad появился уже после создания buh
    buh.save_draft([sklad.settings.station.station_id], "черновик в окне", "", [path])

    window = MainWindow(buh)
    try:
        titles = [window.folders.item(i).text() for i in range(window.folders.count())]
        assert any("Черновики" in t for t in titles)

        index = next(i for i, t in enumerate(titles) if "Черновики" in t)
        window.folders.setCurrentRow(index)
        assert window.current_folder == DRAFTS
        assert window.messages.count() == 1
        assert "черновик в окне" in window.messages.item(0).text()
        assert "Склад" in window.messages.item(0).text()

        window.messages.setCurrentRow(0)
        assert "Черновик" in window.header.text()
        # В папке черновиков кнопки меняют смысл.
        assert window.save_button.text() == "Продолжить письмо"
        assert window.delete_button.text() == "Удалить черновик"
        assert not window.reply_button.isEnabled()
    finally:
        window.tray.hide()


def test_draft_shows_number_for_unknown_station(buh, tmp_path: Path):
    """Станции нет в кэше адресной книги — показываем номер, а не вопросительный знак."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from filepost_client.ui.main_window import DRAFTS, MainWindow

    QApplication.instance() or QApplication([])
    buh.save_draft([9999], "кому-то", "", [])

    window = MainWindow(buh)
    try:
        titles = [window.folders.item(i).text() for i in range(window.folders.count())]
        window.folders.setCurrentRow(next(i for i, t in enumerate(titles) if "Черновики" in t))
        assert window.current_folder == DRAFTS
        assert "станция №9999" in window.messages.item(0).text()
    finally:
        window.tray.hide()


def test_buttons_reset_when_selection_cleared(buh, sklad):
    """Удалили последний черновик — на пустой карточке не должно остаться
    «Продолжить письмо»."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from filepost_client.ui.main_window import DRAFTS, MainWindow

    QApplication.instance() or QApplication([])
    draft_id = buh.save_draft([sklad.settings.station.station_id], "уйдёт", "", [])

    window = MainWindow(buh)
    try:
        assert window.select_folder(DRAFTS)
        window.messages.setCurrentRow(0)
        assert window.save_button.text() == "Продолжить письмо"

        buh.delete_draft(draft_id)
        window._fill_messages()
        assert window.messages.count() == 0
        assert window.save_button.text() == "Сохранить всё"
        assert window.delete_button.text() == "Удалить у себя"
        assert window.reply_button.isEnabled()
    finally:
        window.tray.hide()
