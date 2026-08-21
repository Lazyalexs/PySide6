"""Вкладки: Передачи, Станции, разделы администратора. Разделы 3.2, 3.8."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiError
from ..core import Core
from ..util import human_eta, human_presence, human_size, human_speed

STATE_LABELS = {
    "queued": "В очереди",
    "active": "Передача",
    "verifying": "Проверка контрольной суммы…",
    "paused": "Приостановлено",
    "done": "Готово",
    "error": "Ошибка",
}


def _table(headers: list[str], widths: dict[int, int] | None = None) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column, width in (widths or {}).items():
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, width)
    return table


class TransfersView(QWidget):
    """При гигабайтных файлах эта вкладка нужна не меньше «Входящих» (3.2)."""

    def __init__(self, core: Core) -> None:
        super().__init__()
        self.core = core
        layout = QVBoxLayout(self)

        # Прогресс и состояние фиксированной ширины: там длинные подписи вроде
        # «Проверка контрольной суммы…», и обрезать их нельзя.
        self.table = _table(
            ["Файл", "Направление", "Прогресс", "Состояние"],
            widths={1: 190, 2: 230, 3: 260},
        )
        layout.addWidget(self.table)

        row = QHBoxLayout()
        for text, slot in (
            ("Пауза", self._pause),
            ("Продолжить", self._resume),
            ("Отменить", self._cancel),
            ("Очистить завершённые", self._clear),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

        self.progress_by_id: dict[int, tuple[float, float | None]] = {}

    def note_progress(self, transfer_id: int, speed: float, eta: float | None) -> None:
        self.progress_by_id[transfer_id] = (speed, eta)

    def refresh(self) -> None:
        items = self.core.store.transfers()
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            arrow = "▲ отправка" if item["direction"] == "upload" else "▼ приём"
            peer = item.get("peer") or ""
            self.table.setItem(row, 0, QTableWidgetItem(item["file_name"]))
            self.table.setItem(row, 1, QTableWidgetItem(f"{arrow}  {peer}"))

            bar = QProgressBar()
            bar.setMaximum(100)
            done = item["transferred"]
            total = max(item["size"], 1)
            bar.setValue(int(100 * done / total))
            if item["state"] == "verifying":
                bar.setRange(0, 0)  # неопределённый прогресс на время проверки
            bar.setFormat(f"{human_size(done)} / {human_size(item['size'])}")
            self.table.setCellWidget(row, 2, bar)

            label = STATE_LABELS.get(item["state"], item["state"])
            if item["state"] == "error" and item.get("error"):
                label = item["error"]
            elif item["state"] == "active":
                speed, eta = self.progress_by_id.get(item["id"], (0.0, None))
                if speed:
                    label = f"{human_speed(speed)} · осталось {human_eta(eta)}"
            self.table.setItem(row, 3, QTableWidgetItem(label))

            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, item["id"])

    def _selected(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _pause(self) -> None:
        if (tid := self._selected()) is not None:
            self.core.transfers.pause(tid)
            self.refresh()

    def _resume(self) -> None:
        if (tid := self._selected()) is not None:
            self.core.transfers.resume(tid)
            self.refresh()

    def _cancel(self) -> None:
        if (tid := self._selected()) is not None:
            self.core.transfers.cancel(tid)
            self.refresh()

    def _clear(self) -> None:
        self.core.store.clear_finished()
        self.refresh()


class StationsView(QWidget):
    """Кто есть в системе, кто на связи, где остался старый .exe (2.10)."""

    def __init__(self, core: Core) -> None:
        super().__init__()
        self.core = core
        layout = QVBoxLayout(self)
        self.table = _table(["Станция", "Статус", "Версия"])
        layout.addWidget(self.table)

        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.reload)
        row = QHBoxLayout()
        row.addWidget(refresh)
        row.addStretch()
        layout.addLayout(row)

    def reload(self) -> None:
        self.core._refresh_presence()
        self.refresh()

    def refresh(self) -> None:
        stations = self.core.stations()
        self.table.setRowCount(len(stations))
        for row, station in enumerate(stations):
            mark = "●" if station["online"] else "○"
            self.table.setItem(
                row, 0, QTableWidgetItem(f"{mark}  {station['display_name']}")
            )
            self.table.setItem(
                row,
                1,
                QTableWidgetItem(
                    human_presence(station["online"], station.get("last_seen_at"))
                ),
            )
            self.table.setItem(row, 2, QTableWidgetItem(station.get("client_version") or "—"))


class AdminStationsView(QWidget):
    """Раздел «Станции» администратора: коды регистрации, отзыв ключей (3.8)."""

    def __init__(self, core: Core) -> None:
        super().__init__()
        self.core = core
        layout = QVBoxLayout(self)
        self.table = _table(["Станция", "Статус", "Версия", "Права"])
        layout.addWidget(self.table)

        row = QHBoxLayout()
        for text, slot in (
            ("Добавить станцию", self._enroll),
            ("Переименовать", self._rename),
            ("Сбросить ключ", self._reset),
            ("Отключить", self._disable),
            ("Обновить", self.refresh),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #444;")
        layout.addWidget(self.hint)

    def refresh(self) -> None:
        try:
            stations = self.core.api.admin_stations()
        except ApiError as exc:
            self.hint.setText(exc.message)
            return
        self.table.setRowCount(len(stations))
        for row, station in enumerate(stations):
            status = "отключена" if not station["is_active"] else human_presence(
                station["online"], station.get("last_seen_at")
            )
            self.table.setItem(row, 0, QTableWidgetItem(station["display_name"]))
            self.table.setItem(row, 1, QTableWidgetItem(status))
            self.table.setItem(row, 2, QTableWidgetItem(station.get("client_version") or "—"))
            self.table.setItem(
                row, 3, QTableWidgetItem("администратор" if station["is_admin"] else "")
            )
            self.table.item(row, 0).setData(
                Qt.ItemDataRole.UserRole, station["station_id"]
            )

    def _selected(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)

    def _enroll(self) -> None:
        try:
            result = self.core.api.admin_enrollment()
        except ApiError as exc:
            QMessageBox.warning(self, "FilePost", exc.message)
            return
        self.hint.setText(
            f"Код регистрации: {result['enrollment_code']}\n"
            f"Действует до {result['expires_at']}, вводится один раз при установке "
            f"клиента на новом ПК."
        )

    def _rename(self) -> None:
        station_id = self._selected()
        if station_id is None:
            return
        name, ok = QInputDialog.getText(self, "Переименовать станцию", "Новое название:")
        if not ok or not name.strip():
            return
        try:
            self.core.api.admin_patch_station(station_id, display_name=name.strip())
        except ApiError as exc:
            QMessageBox.warning(self, "FilePost", exc.message)
        self.refresh()

    def _reset(self) -> None:
        station_id = self._selected()
        if station_id is None:
            return
        confirm = QMessageBox.question(
            self,
            "FilePost",
            "Отозвать ключ станции? Она перестанет подключаться, пока не будет "
            "зарегистрирована заново по новому коду.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.core.api.admin_reset_station(station_id)
        except ApiError as exc:
            QMessageBox.warning(self, "FilePost", exc.message)
            return
        self.hint.setText(f"Ключ отозван. Новый код регистрации: {result['enrollment_code']}")
        self.refresh()

    def _disable(self) -> None:
        station_id = self._selected()
        if station_id is None:
            return
        try:
            self.core.api.admin_patch_station(station_id, is_active=False)
        except ApiError as exc:
            QMessageBox.warning(self, "FilePost", exc.message)
        self.refresh()


class AdminStorageView(QWidget):
    """Главный рабочий инструмент администратора: автоочистка выключена (3.8)."""

    def __init__(self, core: Core) -> None:
        super().__init__()
        self.core = core
        layout = QVBoxLayout(self)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.table = _table(["Категория", "Файлов"], widths={1: 120})
        layout.addWidget(self.table)

        row = QHBoxLayout()
        for text, slot in (
            ("Обновить", self.refresh),
            ("Сверить БД и диск", self._verify),
            ("Запустить уборку", self._sweep),
            ("Резервная копия сейчас", self._backup),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

        self.log = QLabel()
        self.log.setWordWrap(True)
        self.log.setStyleSheet("color: #444;")
        layout.addWidget(self.log)

    def refresh(self) -> None:
        try:
            data = self.core.api.admin_storage()
        except ApiError as exc:
            self.summary.setText(exc.message)
            return

        warning = "  ⚠ НИЖЕ ПОРОГА" if data["below_threshold"] else ""
        self.summary.setText(
            f"Свободно {human_size(data['free_space'])}{warning} · "
            f"обещано заливкам {human_size(data['reserved'])} · "
            f"занято вложениями {human_size(data['used_by_attachments'])} · "
            f"порог {human_size(data['min_free_space'])}"
        )

        # Сверху то, что удалять безопаснее всего: ничейные не видны уже никому.
        rows = [
            ("Ничейные (скрыты всеми участниками)", len(data["orphaned"])),
            ("Потерянные файлы (нет на диске)", len(data["missing"])),
        ]
        self.table.setRowCount(len(rows))
        for row, (name, count) in enumerate(rows):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(str(count)))

    def _verify(self) -> None:
        try:
            report = self.core.api.admin_verify()
        except ApiError as exc:
            self.log.setText(exc.message)
            return
        self.log.setText(
            f"Проверено записей: {report['checked']}. "
            f"Записей без файла: {len(report['missing_files'])}. "
            f"Файлов без записи: {len(report['orphan_files'])}."
        )
        self.refresh()

    def _sweep(self) -> None:
        try:
            report = self.core.api.admin_housekeeping()
        except ApiError as exc:
            self.log.setText(exc.message)
            return
        self.log.setText(
            f"Убрано брошенных загрузок: {report['abandoned_uploads']}, "
            f"обрезано событий: {report['events_trimmed']}, "
            f"помечено ничейными: {len(report['orphaned_marked'])}."
        )
        self.refresh()

    def _backup(self) -> None:
        try:
            report = self.core.api.admin_housekeeping(backup=True)
        except ApiError as exc:
            self.log.setText(exc.message)
            return
        self.log.setText(
            f"Резервная копия: {report.get('backup_path') or 'не создана'}"
        )


class AdminAuditView(QWidget):
    def __init__(self, core: Core) -> None:
        super().__init__()
        self.core = core
        layout = QVBoxLayout(self)
        self.table = _table(["Время", "Станция", "Действие", "Подробности"])
        layout.addWidget(self.table)
        button = QPushButton("Обновить")
        button.clicked.connect(self.refresh)
        row = QHBoxLayout()
        row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

    def refresh(self) -> None:
        try:
            entries = self.core.api.admin_audit()
        except ApiError as exc:
            QMessageBox.warning(self, "FilePost", exc.message)
            return
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(entry["timestamp"]))
            self.table.setItem(row, 1, QTableWidgetItem(entry.get("station") or "—"))
            self.table.setItem(row, 2, QTableWidgetItem(entry["action"]))
            self.table.setItem(row, 3, QTableWidgetItem(entry.get("details") or ""))
