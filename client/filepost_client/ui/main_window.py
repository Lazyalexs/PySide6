"""Главное окно. Раздел 3.2.

Три правила, которым подчинено всё: ничего не вводить руками, показывать
человеческие имена, говорить по-русски в ошибках.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiError
from ..core import Core
from ..sound import play_notification
from ..transfers import Progress
from ..util import human_size, human_time
from .compose import ComposeDialog
from .settings_dialog import SettingsDialog
from .views import (
    AdminAuditView,
    AdminStationsView,
    AdminStorageView,
    StationsView,
    TransfersView,
)

INBOX, SENT, DRAFTS = "inbox", "sent", "drafts"
TRANSFERS, STATIONS = "transfers", "stations"
ADMIN_STATIONS, ADMIN_STORAGE, ADMIN_AUDIT = "a_stations", "a_storage", "a_audit"

#: Папки, где в средней колонке показывается список писем.
LIST_FOLDERS = (INBOX, SENT, DRAFTS)


def _icon(color: str) -> QIcon:
    pixmap = QPixmap(16, 16)
    pixmap.fill(color)
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    #: Ядро работает в своих потоках, поэтому в UI всё заводится через сигналы.
    state_changed = Signal()
    message_arrived = Signal(dict)
    progress_changed = Signal(object)

    def __init__(self, core: Core) -> None:
        super().__init__()
        self.core = core
        self.current_folder = INBOX
        self.setWindowTitle("FilePost")
        self.resize(1100, 680)

        self._build_ui()
        self._build_tray()

        core.on_state_change = self.state_changed.emit
        core.on_new_message = self.message_arrived.emit
        core.on_transfer_progress = self.progress_changed.emit
        self.state_changed.connect(self.refresh)
        self.message_arrived.connect(self._on_new_message)
        self.progress_changed.connect(self._on_progress)

        # Передачи обновляются чаще списка писем: там меняются проценты.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self.refresh()

    # ------------------------------------------------------------------ построение

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.folders = QListWidget()
        self.folders.setMaximumWidth(210)
        self.folders.currentItemChanged.connect(self._folder_changed)
        splitter.addWidget(self.folders)

        self.messages = QListWidget()
        self.messages.currentItemChanged.connect(self._message_selected)
        splitter.addWidget(self.messages)

        self.right = QStackedWidget()
        self.right.addWidget(self._message_pane())
        self.transfers_view = TransfersView(self.core)
        self.stations_view = StationsView(self.core)
        self.admin_stations = AdminStationsView(self.core)
        self.admin_storage = AdminStorageView(self.core)
        self.admin_audit = AdminAuditView(self.core)
        for widget in (
            self.transfers_view,
            self.stations_view,
            self.admin_stations,
            self.admin_storage,
            self.admin_audit,
        ):
            self.right.addWidget(widget)
        splitter.addWidget(self.right)

        splitter.setSizes([200, 320, 580])
        self.setCentralWidget(splitter)

        toolbar = self.addToolBar("Действия")
        toolbar.setMovable(False)
        new_action = QAction("Новое сообщение", self)
        new_action.triggered.connect(self._compose)
        toolbar.addAction(new_action)

        refresh_action = QAction("Обновить", self)
        refresh_action.triggered.connect(self._force_refresh)
        toolbar.addAction(refresh_action)

        settings_action = QAction("Настройки", self)
        settings_action.triggered.connect(self._open_settings)
        toolbar.addAction(settings_action)

        self.status = QLabel()
        self.statusBar().addWidget(self.status)
        self._fill_folders()

    def _message_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)

        self.header = QLabel()
        self.header.setTextFormat(Qt.TextFormat.RichText)
        self.header.setWordWrap(True)
        layout.addWidget(self.header)

        self.body = QTextBrowser()
        layout.addWidget(self.body)

        self.attachments = QListWidget()
        self.attachments.setMaximumHeight(160)
        layout.addWidget(self.attachments)

        row = QHBoxLayout()
        self.save_button = QPushButton("Сохранить всё")
        self.save_button.clicked.connect(self._download_all)
        self.reply_button = QPushButton("Ответить")
        self.reply_button.clicked.connect(self._reply)
        self.delete_button = QPushButton("Удалить у себя")
        self.delete_button.clicked.connect(self._hide)
        for button in (self.save_button, self.reply_button, self.delete_button):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        return pane

    def _fill_folders(self) -> None:
        self.folders.clear()
        entries = [
            (INBOX, "✉  Входящие"),
            (SENT, "➤  Отправленные"),
            (DRAFTS, "✎  Черновики"),
            (TRANSFERS, "⇅  Передачи"),
            (STATIONS, "🖥  Станции"),
        ]
        if self.core.settings.station.is_admin:
            entries += [
                (None, "— УПРАВЛЕНИЕ —"),
                (ADMIN_STATIONS, "    Станции"),
                (ADMIN_STORAGE, "    Хранилище"),
                (ADMIN_AUDIT, "    Журнал"),
            ]
        for key, title in entries:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, key)
            if key is None:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.folders.addItem(item)
        self.folders.setCurrentRow(0)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(_icon("#2d6cdf"), self)
        menu = QMenu()
        show = menu.addAction("Открыть FilePost")
        show.triggered.connect(self._restore)
        compose = menu.addAction("Новое сообщение")
        compose.triggered.connect(self._compose)
        menu.addSeparator()
        quit_action = menu.addAction("Выход")
        quit_action.triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._restore()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick
            else None
        )
        self.tray.show()

    # ------------------------------------------------------------------ обновление

    def _tick(self) -> None:
        if self.current_folder == TRANSFERS:
            self.transfers_view.refresh()
        self._update_status()

    def refresh(self) -> None:
        unread = self.core.unread_count()
        self.setWindowTitle(f"FilePost — {unread} непрочитанных" if unread else "FilePost")
        self.folders.item(0).setText(
            f"✉  Входящие {unread}" if unread else "✉  Входящие"
        )
        self.tray.setToolTip(f"FilePost — {unread} непрочитанных" if unread else "FilePost")
        if self.current_folder in LIST_FOLDERS:
            self._fill_messages()
        self._update_status()

    def _update_status(self) -> None:
        settings = self.core.settings
        if self.core.online:
            free = self.core.health.get("free_space_human", "")
            suffix = f" · свободно {free}" if free else ""
            self.status.setText(
                f"●  Сервер доступен · {settings.station.display_name}{suffix}"
            )
            self.status.setStyleSheet("color: #1a7f37;")
        else:
            # Вместо тишины и непонимания — что именно не так (3.2).
            self.status.setText(f"●  {self.core.last_error or 'Нет связи с сервером'}")
            self.status.setStyleSheet("color: #c00;")

    def _fill_messages(self) -> None:
        current = self._selected_message_id()
        self.messages.clear()

        if self.current_folder == DRAFTS:
            self._fill_drafts()
            return

        items = self.core.inbox() if self.current_folder == INBOX else self.core.sent()
        for item in items:
            peer = (
                item.get("sender")
                if self.current_folder == INBOX
                else ", ".join(r["name"] for r in item.get("recipients", []))
            )
            marks = f"  📎{len(item['attachments'])}" if item["attachments"] else ""
            entry = QListWidgetItem(
                f"{peer or '—'}     {human_time(item.get('sent_at'))}\n"
                f"{item.get('subject') or '(без темы)'}{marks}"
            )
            entry.setData(Qt.ItemDataRole.UserRole, item["id"])
            if self.current_folder == INBOX and not item["is_read"]:
                font = QFont()
                font.setBold(True)
                entry.setFont(font)
            self.messages.addItem(entry)
            if item["id"] == current:
                self.messages.setCurrentItem(entry)

    def _fill_drafts(self) -> None:
        names = self._station_names()
        for draft in self.core.drafts():
            peer = ", ".join(names(r) for r in draft["recipients"]) or "(без адресата)"
            marks = f"  📎{len(draft['files'])}" if draft["files"] else ""
            entry = QListWidgetItem(
                f"{peer}     {human_time(draft.get('updated_at'))}\n"
                f"{draft.get('subject') or '(без темы)'}{marks}"
            )
            entry.setData(Qt.ItemDataRole.UserRole, draft["id"])
            self.messages.addItem(entry)

    # ------------------------------------------------------------------ события UI

    def _folder_changed(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if key is None:
            return
        self.current_folder = key
        pages = {
            INBOX: 0,
            SENT: 0,
            DRAFTS: 0,
            TRANSFERS: 1,
            STATIONS: 2,
            ADMIN_STATIONS: 3,
            ADMIN_STORAGE: 4,
            ADMIN_AUDIT: 5,
        }
        self.right.setCurrentIndex(pages.get(key, 0))
        self.messages.setVisible(key in LIST_FOLDERS)

        if key in LIST_FOLDERS:
            self._fill_messages()
        elif key == TRANSFERS:
            self.transfers_view.refresh()
        elif key == STATIONS:
            self.stations_view.refresh()
        elif key == ADMIN_STATIONS:
            self.admin_stations.refresh()
        elif key == ADMIN_STORAGE:
            self.admin_storage.refresh()
        elif key == ADMIN_AUDIT:
            self.admin_audit.refresh()

    def select_folder(self, key: str) -> bool:
        """Переключиться на раздел по ключу, а не по номеру строки.

        Номера сдвигаются при добавлении разделов, и у администратора список
        длиннее — жёсткие индексы здесь ломаются молча.
        """
        for i in range(self.folders.count()):
            if self.folders.item(i).data(Qt.ItemDataRole.UserRole) == key:
                self.folders.setCurrentRow(i)
                return True
        return False

    def _selected_message_id(self) -> int | None:
        item = self.messages.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _message_selected(self, item: QListWidgetItem | None) -> None:
        if item is None:
            self.header.clear()
            self.body.clear()
            self.attachments.clear()
            # Кнопки тоже сбрасываем: иначе после удаления последнего черновика
            # на пустой карточке остаётся «Продолжить письмо».
            self._set_buttons_for_message()
            return

        if self.current_folder == DRAFTS:
            self._show_draft(item.data(Qt.ItemDataRole.UserRole))
            return

        message = self.core.message(item.data(Qt.ItemDataRole.UserRole))
        if not message:
            return

        self._set_buttons_for_message()

        recipients = ", ".join(r["name"] for r in message.get("recipients", []))
        lines = [f"<b>Тема:</b> {message.get('subject') or '(без темы)'}"]
        if message.get("sender"):
            lines.insert(0, f"<b>От:</b> {message['sender']}")
        if recipients:
            lines.append(f"<b>Кому:</b> {recipients}")
        lines.append(human_time(message.get("sent_at")))
        self.header.setText("<br>".join(lines))
        self.body.setPlainText(message.get("body") or "")

        self.attachments.clear()
        total = 0
        for attachment in message.get("attachments", []):
            total += attachment["size"]
            state = "" if attachment["state"] == "ready" else f"   ({attachment['state']})"
            self.attachments.addItem(
                f"📎  {attachment['original_name']}     "
                f"{human_size(attachment['size'])}{state}"
            )
        if total:
            self.attachments.insertItem(0, f"Вложения ({human_size(total)}):")

        if self.current_folder == INBOX and not message["is_read"]:
            self.core.mark_read(message["id"])

    def _set_buttons_for_message(self) -> None:
        self.save_button.setText("Сохранить всё")
        self.reply_button.setEnabled(True)
        self.delete_button.setText("Удалить у себя")

    def _set_buttons_for_draft(self) -> None:
        """В папке черновиков те же кнопки означают другое."""
        self.save_button.setText("Продолжить письмо")
        self.reply_button.setEnabled(False)
        self.delete_button.setText("Удалить черновик")

    def _station_names(self):
        """Имена из кэша адресной книги.

        Станция может отсутствовать: черновик пролежал дольше, чем станция была
        в системе, или кэш ещё не обновлялся. «станция №4» понятнее вопросительного
        знака — по номеру администратор хотя бы найдёт, о ком речь.
        """
        cache = {s["station_id"]: s["display_name"] for s in self.core.stations()}
        return lambda station_id: cache.get(station_id, f"станция №{station_id}")

    def _show_draft(self, draft_id: int) -> None:
        draft = self.core.draft(draft_id)
        if not draft:
            return
        names = self._station_names()
        peer = ", ".join(names(r) for r in draft["recipients"]) or "—"
        self.header.setText(
            f"<b>Черновик</b><br><b>Кому:</b> {peer}<br>"
            f"<b>Тема:</b> {draft.get('subject') or '(без темы)'}"
        )
        self.body.setPlainText(draft.get("body") or "")
        self.attachments.clear()
        for path in draft["files"]:
            exists = Path(path).exists()
            suffix = "" if exists else "   (файл не найден)"
            self.attachments.addItem(f"📎  {Path(path).name}{suffix}")

        self._set_buttons_for_draft()

    def _edit_draft(self) -> None:
        draft_id = self._selected_message_id()
        if draft_id is None:
            return
        draft = self.core.draft(draft_id)
        if not draft:
            return
        dialog = ComposeDialog(self.core, self, draft=draft)
        dialog.exec()
        self.refresh()
        self._fill_messages()

    def _on_new_message(self, message: dict) -> None:
        if self.core.settings.prefs.notify_new_message:
            self.tray.showMessage(
                f"Новое сообщение от {message.get('sender', '')}",
                message.get("subject") or "(без темы)",
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )
            play_notification(self.core.settings.prefs.sound)
        self.refresh()

    def _on_progress(self, progress: Progress) -> None:
        self.transfers_view.note_progress(progress.transfer_id, progress.speed, progress.eta)
        if progress.state == "done" and self.core.settings.prefs.notify_transfer_done:
            self.tray.showMessage(
                "Передача завершена", "", QSystemTrayIcon.MessageIcon.Information, 3000
            )
            play_notification(self.core.settings.prefs.sound)
        if progress.state == "error" and progress.error:
            self.tray.showMessage(
                "Ошибка передачи", progress.error, QSystemTrayIcon.MessageIcon.Warning, 8000
            )
            play_notification(self.core.settings.prefs.sound)

    # ------------------------------------------------------------------ действия

    def _compose(self, reply_to: dict | None = None) -> None:
        dialog = ComposeDialog(self.core, self, reply_to=reply_to or None)
        if dialog.exec():
            self.select_folder(TRANSFERS)  # там видно, что передача пошла

    def _reply(self) -> None:
        message_id = self._selected_message_id()
        if message_id is None:
            return
        self._compose(self.core.message(message_id))

    def _download_all(self) -> None:
        # В папке черновиков та же кнопка продолжает письмо, а не качает вложения.
        if self.current_folder == DRAFTS:
            self._edit_draft()
            return
        message_id = self._selected_message_id()
        if message_id is None:
            return
        ids = self.core.download_all(message_id)
        if not ids:
            QMessageBox.information(self, "FilePost", "В сообщении нет готовых вложений.")
            return
        self.select_folder(TRANSFERS)

    def _hide(self) -> None:
        item_id = self._selected_message_id()
        if item_id is None:
            return

        if self.current_folder == DRAFTS:
            confirm = QMessageBox.question(self, "FilePost", "Удалить черновик?")
            if confirm == QMessageBox.StandardButton.Yes:
                self.core.delete_draft(item_id)
                self._fill_messages()
            return

        confirm = QMessageBox.question(
            self, "FilePost", "Скрыть это сообщение у себя?"
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.core.hide(item_id)
            self.refresh()

    def _force_refresh(self) -> None:
        try:
            self.core.refresh_all()
            self.core.refresh_health()
        except ApiError as exc:
            QMessageBox.warning(self, "FilePost", exc.message)
        self.refresh()

    def _open_settings(self) -> None:
        if SettingsDialog(self.core, self).exec():
            self.refresh()

    def open_downloads(self) -> None:
        path = self.core.settings.downloads_dir
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    # ------------------------------------------------------------------ окно и трей

    def _restore(self) -> None:
        self.showNormal()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt
        """Закрытие сворачивает в трей и не прерывает передачи (3.2)."""
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "FilePost продолжает работать",
            "Программа свёрнута в трей. Передачи продолжаются.",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

    def _quit(self) -> None:
        active = self.core.store.transfers(["queued", "active", "verifying"])
        if active:
            confirm = QMessageBox.question(
                self,
                "FilePost",
                f"Идёт передач: {len(active)}. Выйти? Они продолжатся при следующем запуске.",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self.tray.hide()
        self.core.stop()
        QApplication.quit()
