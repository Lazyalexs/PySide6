"""Дымовые тесты интерфейса: окна строятся и наполняются настоящими данными.

Запускаются на offscreen-платформе Qt — окна не показываются, но виджеты создаются
и заполняются по-настоящему.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from conftest import wait_for

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from filepost_client.ui.compose import ComposeDialog  # noqa: E402
from filepost_client.ui.main_window import INBOX, STATIONS, TRANSFERS, MainWindow  # noqa: E402
from filepost_client.ui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def make_file(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    return path


def test_main_window_builds(qt_app, buh, sklad):
    buh.sync()
    window = MainWindow(buh)
    try:
        assert window.windowTitle().startswith("FilePost")
        # Разделы: 4 обычных + разделитель + 3 админских (станция администратора).
        assert window.folders.count() == 8
        assert "Сервер доступен" in window.status.text()
        assert buh.settings.station.display_name in window.status.text()
    finally:
        window.tray.hide()


def test_non_admin_has_no_management_sections(qt_app, sklad):
    window = MainWindow(sklad)
    try:
        titles = [window.folders.item(i).text() for i in range(window.folders.count())]
        assert not any("УПРАВЛЕНИЕ" in t for t in titles)
        assert window.folders.count() == 4
    finally:
        window.tray.hide()


def test_offline_banner(qt_app, buh, server):
    server.stop()
    buh.sync()
    window = MainWindow(buh)
    try:
        # Не «HTTP 0», а понятный текст (3.2).
        assert "Нет связи с сервером" in window.status.text()
        assert "#c00" in window.status.styleSheet()
    finally:
        window.tray.hide()


def test_compose_lists_stations_not_addresses(qt_app, buh, sklad):
    buh.sync()
    dialog = ComposeDialog(buh)
    labels = [dialog.recipients.item(i).text() for i in range(dialog.recipients.count())]
    assert any("Склад" in label for label in labels)
    # Ни IP, ни имени ПК в списке быть не должно.
    assert not any("127.0.0.1" in label or "SKLAD" in label.upper() for label in labels)
    assert any("в сети" in label for label in labels)


def test_compose_filter(qt_app, buh, sklad, make_client):
    make_client("Кадры")
    buh._refresh_presence()
    dialog = ComposeDialog(buh)
    dialog.filter.setText("склад")
    visible = [
        dialog.recipients.item(i).text()
        for i in range(dialog.recipients.count())
        if not dialog.recipients.item(i).isHidden()
    ]
    assert len(visible) == 1 and "Склад" in visible[0]


def test_inbox_shows_message(qt_app, buh, sklad, tmp_path: Path):
    path = make_file(tmp_path / "src" / "акты.zip", 4096)
    buh.transfers.start()
    buh.compose([sklad.settings.station.station_id], "Акты за июль", "до пятницы", [path])
    assert wait_for(
        lambda: (t := buh.store.transfers()[0])["state"] == "done", timeout=60
    ), buh.store.transfers()[0]
    assert wait_for(lambda: sklad.sync() or sklad.inbox())

    window = MainWindow(sklad)
    try:
        window.refresh()
        assert window.messages.count() == 1
        text = window.messages.item(0).text()
        assert "Акты за июль" in text
        assert "Бухгалтерия, окно 2" in text
        assert "📎1" in text
        # Непрочитанное — жирным (3.2).
        assert window.messages.item(0).font().bold()

        window.messages.setCurrentRow(0)
        assert "Акты за июль" in window.header.text()
        assert "до пятницы" in window.body.toPlainText()
        assert any(
            "акты.zip" in window.attachments.item(i).text()
            for i in range(window.attachments.count())
        )
        # Открыли — счётчик непрочитанных обнулился.
        assert sklad.unread_count() == 0
    finally:
        window.tray.hide()


def test_transfers_view_shows_progress(qt_app, buh, sklad, tmp_path: Path):
    path = make_file(tmp_path / "src" / "big.bin", 2 * 1024 * 1024)
    buh.transfers.start()
    buh.compose([sklad.settings.station.station_id], "t", "", [path])

    window = MainWindow(buh)
    try:
        window.folders.setCurrentRow(2)
        assert window.current_folder == TRANSFERS
        window.transfers_view.refresh()
        assert window.transfers_view.table.rowCount() == 1
        assert "big.bin" in window.transfers_view.table.item(0, 0).text()
        assert "отправка" in window.transfers_view.table.item(0, 1).text()
    finally:
        window.tray.hide()


def test_stations_view(qt_app, buh, sklad):
    buh._refresh_presence()
    window = MainWindow(buh)
    try:
        window.folders.setCurrentRow(3)
        assert window.current_folder == STATIONS
        window.stations_view.refresh()
        assert window.stations_view.table.rowCount() == 1
        assert "Склад" in window.stations_view.table.item(0, 0).text()
        assert window.stations_view.table.item(0, 1).text() == "в сети"
    finally:
        window.tray.hide()


def test_admin_views_load(qt_app, buh, sklad):
    window = MainWindow(buh)
    try:
        window.admin_stations.refresh()
        assert window.admin_stations.table.rowCount() == 2

        window.admin_storage.refresh()
        assert "Свободно" in window.admin_storage.summary.text()

        window.admin_audit.refresh()
        assert window.admin_audit.table.rowCount() > 0
    finally:
        window.tray.hide()


def test_admin_enrollment_shows_code(qt_app, buh):
    window = MainWindow(buh)
    try:
        window.admin_stations._enroll()
        assert "Код регистрации:" in window.admin_stations.hint.text()
    finally:
        window.tray.hide()


def test_settings_dialog_locks_deployment_params(qt_app, buh):
    """Параметры развёртывания показываются, но не редактируются (3.7)."""
    dialog = SettingsDialog(buh)
    groups = [
        dialog.layout().itemAt(i).widget().title()
        for i in range(dialog.layout().count())
        if hasattr(dialog.layout().itemAt(i).widget(), "title")
    ]
    assert any("Задано администратором" in g for g in groups)
    # Имя станции редактируемое, адрес сервера — нет (это QLabel, не QLineEdit).
    assert dialog.display_name.isEnabled()
    assert not hasattr(dialog, "server_url_edit")


def test_settings_saves_preferences(qt_app, buh):
    dialog = SettingsDialog(buh)
    dialog.auto_download.setChecked(True)
    dialog.parallel.setValue(1)
    dialog._save()

    from filepost_client.settings import Settings

    reloaded = Settings(root=buh.settings.root).load()
    assert reloaded.prefs.auto_download is True
    assert reloaded.prefs.parallel_transfers == 1
