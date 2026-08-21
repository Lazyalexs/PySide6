"""Окно настроек. Раздел 3.7.

Параметры развёртывания показываются серыми с подписью «Задано администратором» —
сюда попадает всё, чем можно сломать себе клиент.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..api import ApiError
from ..core import VERSION, Core


class SettingsDialog(QDialog):
    def __init__(self, core: Core, parent=None) -> None:
        super().__init__(parent)
        self.core = core
        self.settings = core.settings
        self.setWindowTitle("Настройки")
        self.resize(560, 720)

        layout = QVBoxLayout(self)
        layout.addWidget(self._station_group())
        layout.addWidget(self._receive_group())
        layout.addWidget(self._transfer_group())
        layout.addWidget(self._notify_group())
        layout.addWidget(self._startup_group())
        layout.addWidget(self._server_group())
        layout.addStretch()

        row = QHBoxLayout()
        diagnostics = QPushButton("Диагностика…")
        diagnostics.clicked.connect(self._show_diagnostics)
        row.addWidget(diagnostics)
        row.addStretch()
        layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ группы

    def _station_group(self) -> QGroupBox:
        box = QGroupBox("Эта станция")
        form = QFormLayout(box)
        self.display_name = QLineEdit(self.settings.station.display_name)
        form.addRow("Отображаемое имя:", self.display_name)
        hint = QLabel("Под этим именем вас видят остальные и на него отправляют файлы")
        hint.setStyleSheet("color: #666;")
        hint.setWordWrap(True)
        form.addRow("", hint)
        return box

    def _receive_group(self) -> QGroupBox:
        box = QGroupBox("Получение файлов")
        form = QFormLayout(box)

        row = QHBoxLayout()
        self.downloads = QLineEdit(str(self.settings.downloads_dir))
        pick = QPushButton("Выбрать…")
        pick.clicked.connect(self._pick_folder)
        row.addWidget(self.downloads)
        row.addWidget(pick)
        form.addRow("Папка загрузок:", row)

        self.auto_download = QCheckBox("Скачивать вложения автоматически")
        self.auto_download.setChecked(self.settings.prefs.auto_download)
        self.auto_download.setToolTip(
            "При гигабайтных файлах решение «забирать или нет» лучше принимать вручную"
        )
        form.addRow("", self.auto_download)

        self.open_after = QCheckBox("Открывать папку после сохранения")
        self.open_after.setChecked(self.settings.prefs.open_folder_after_save)
        form.addRow("", self.open_after)

        self.clash = QComboBox()
        self.clash.addItems(["переименовать", "заменять"])
        self.clash.setCurrentIndex(0 if self.settings.prefs.on_name_clash == "rename" else 1)
        form.addRow("При совпадении имён:", self.clash)
        return box

    def _transfer_group(self) -> QGroupBox:
        box = QGroupBox("Передача")
        form = QFormLayout(box)

        self.parallel = QSpinBox()
        self.parallel.setRange(1, 4)
        self.parallel.setValue(self.settings.prefs.parallel_transfers)
        self.parallel.setToolTip(
            "Больше двух смысла не имеет: потоки делят один и тот же канал"
        )
        form.addRow("Одновременных передач:", self.parallel)

        self.limit = QSpinBox()
        self.limit.setRange(0, 10000)
        self.limit.setSuffix(" МБ/с (0 — без ограничения)")
        self.limit.setValue(self.settings.prefs.upload_limit_mbps)
        form.addRow("Ограничение отдачи:", self.limit)
        return box

    def _notify_group(self) -> QGroupBox:
        box = QGroupBox("Уведомления")
        form = QFormLayout(box)
        self.notify_new = QCheckBox("О новом сообщении")
        self.notify_new.setChecked(self.settings.prefs.notify_new_message)
        self.notify_done = QCheckBox("О завершении передачи")
        self.notify_done.setChecked(self.settings.prefs.notify_transfer_done)
        self.sound = QCheckBox("Звук")
        self.sound.setChecked(self.settings.prefs.sound)
        for widget in (self.notify_new, self.notify_done, self.sound):
            form.addRow("", widget)
        return box

    def _startup_group(self) -> QGroupBox:
        box = QGroupBox("Запуск")
        form = QFormLayout(box)
        self.autostart = QCheckBox("Запускать вместе с Windows")
        self.autostart.setChecked(self.settings.prefs.autostart)
        self.minimized = QCheckBox("Стартовать свёрнутым в трей")
        self.minimized.setChecked(self.settings.prefs.start_minimized)
        for widget in (self.autostart, self.minimized):
            form.addRow("", widget)
        return box

    def _server_group(self) -> QGroupBox:
        box = QGroupBox("Сервер          🔒 Задано администратором")
        form = QFormLayout(box)
        for label, value in (
            ("Адрес:", self.settings.server.url or "—"),
            ("Запасной адрес:", self.settings.server.fallback or "—"),
            ("Таймаут:", f"{self.settings.server.timeout_sec} с"),
            ("Опрос событий:", f"{self.settings.server.poll_interval_sec} с"),
        ):
            widget = QLabel(value)
            widget.setStyleSheet("color: #888;")
            form.addRow(label, widget)

        row = QHBoxLayout()
        check = QPushButton("Проверить соединение")
        check.clicked.connect(self._check)
        self.check_result = QLabel()
        row.addWidget(check)
        row.addWidget(self.check_result)
        row.addStretch()
        form.addRow("", row)
        return box

    # ------------------------------------------------------------------ действия

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка загрузок", self.downloads.text())
        if path:
            self.downloads.setText(path)

    def _check(self) -> None:
        health = self.core.refresh_health()
        if health:
            self.check_result.setText(f"● Доступен, свободно {health['free_space_human']}")
            self.check_result.setStyleSheet("color: green;")
        else:
            self.check_result.setText("● Недоступен")
            self.check_result.setStyleSheet("color: #c00;")

    def _show_diagnostics(self) -> None:
        """Всё техническое убрано сюда — обычный пользователь этого не видит (3.7)."""
        settings = self.settings
        text = "\n".join(
            [
                f"Версия клиента:     {VERSION}",
                f"Идентификатор станции: {settings.station.station_id}",
                f"Имя станции:        {settings.station.display_name}",
                f"Адрес сервера:      {self.core.api.base_url or '—'}",
                f"Состояние:          {'на связи' if self.core.online else self.core.last_error}",
                f"Курсор событий:     {self.core.store.last_event_id}",
                f"Каталог данных:     {settings.root}",
                f"Журнал:             {settings.logs_dir / 'client.log'}",
            ]
        )
        box = QMessageBox(self)
        box.setWindowTitle("Диагностика")
        box.setText("Сведения для администратора")
        box.setDetailedText(text)
        copy = box.addButton("Скопировать сведения", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Закрыть", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is copy:
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(text)

    def _save(self) -> None:
        prefs = self.settings.prefs
        prefs.downloads_dir = self.downloads.text().strip()
        prefs.auto_download = self.auto_download.isChecked()
        prefs.open_folder_after_save = self.open_after.isChecked()
        prefs.on_name_clash = "rename" if self.clash.currentIndex() == 0 else "replace"
        prefs.parallel_transfers = self.parallel.value()
        prefs.upload_limit_mbps = self.limit.value()
        prefs.notify_new_message = self.notify_new.isChecked()
        prefs.notify_transfer_done = self.notify_done.isChecked()
        prefs.sound = self.sound.isChecked()
        prefs.autostart = self.autostart.isChecked()
        prefs.start_minimized = self.minimized.isChecked()

        new_name = self.display_name.text().strip()
        if new_name and new_name != self.settings.station.display_name:
            try:
                self.core.rename_station(new_name)
            except ApiError as exc:
                QMessageBox.warning(self, "FilePost", exc.message)
                return

        self.settings.save()
        self.core.apply_settings()
        self.accept()
