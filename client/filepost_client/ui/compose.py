"""Окно «Новое сообщение». Получатели выбираются из списка, ничего не вводится (3.2)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from ..api import ApiError
from ..core import Core
from ..util import human_eta, human_presence, human_size


class ComposeDialog(QDialog):
    def __init__(
        self,
        core: Core,
        parent=None,
        reply_to: dict | None = None,
        draft: dict | None = None,
    ) -> None:
        super().__init__(parent)
        self.core = core
        self.files: list[Path] = []
        self.draft_id: int | None = draft["id"] if draft else None
        self.setWindowTitle("Новое сообщение")
        self.resize(680, 620)
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)

        # --- получатели: набор с клавиатуры фильтрует список, а не вводит адрес
        layout.addWidget(QLabel("Кому:"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Поиск по названию станции…")
        self.filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter)

        self.recipients = QListWidget()
        self.recipients.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.recipients.setMaximumHeight(150)
        layout.addWidget(self.recipients)
        self._fill_recipients()

        form = QFormLayout()
        self.subject = QLineEdit()
        form.addRow("Тема:", self.subject)
        layout.addLayout(form)

        self.body = QTextEdit()
        self.body.setPlaceholderText("Комментарий к передаче…")
        self.body.setMaximumHeight(110)
        layout.addWidget(self.body)

        layout.addWidget(QLabel("Файлы:"))
        self.file_list = QListWidget()
        layout.addWidget(self.file_list)

        row = QHBoxLayout()
        add = QPushButton("Выбрать файлы…")
        add.clicked.connect(self._pick_files)
        remove = QPushButton("Убрать")
        remove.clicked.connect(self._remove_file)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)

        self.summary = QLabel("Перетащите файлы сюда или нажмите «Выбрать файлы»")
        self.summary.setStyleSheet("color: #666;")
        layout.addWidget(self.summary)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Отправить")
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("В черновики")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._send)
        buttons.button(QDialogButtonBox.StandardButton.Save).clicked.connect(self._save_draft)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if reply_to:
            self.subject.setText(f"Re: {reply_to.get('subject', '')}")
            self._preselect(reply_to.get("sender"))
        if draft:
            self._load_draft(draft)

    def _load_draft(self, draft: dict) -> None:
        self.subject.setText(draft.get("subject", ""))
        self.body.setPlainText(draft.get("body", ""))
        wanted = set(draft.get("recipients", []))
        for i in range(self.recipients.count()):
            item = self.recipients.item(i)
            if item.data(Qt.ItemDataRole.UserRole) in wanted:
                item.setSelected(True)
        # Файл мог быть удалён или перемещён с момента сохранения черновика.
        missing = [p for p in draft.get("files", []) if not Path(p).exists()]
        self._add_files([Path(p) for p in draft.get("files", []) if Path(p).exists()])
        if missing:
            self.summary.setText(
                f"Не найдено файлов: {len(missing)} — добавьте заново"
            )

    # ------------------------------------------------------------------ получатели

    def _fill_recipients(self) -> None:
        self.recipients.clear()
        for station in self.core.stations():
            mark = "●" if station["online"] else "○"
            presence = human_presence(station["online"], station.get("last_seen_at"))
            item = QListWidgetItem(f"{mark}  {station['display_name']}     {presence}")
            item.setData(Qt.ItemDataRole.UserRole, station["station_id"])
            self.recipients.addItem(item)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.recipients.count()):
            item = self.recipients.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _preselect(self, display_name: str | None) -> None:
        if not display_name:
            return
        for i in range(self.recipients.count()):
            item = self.recipients.item(i)
            if display_name in item.text():
                item.setSelected(True)

    def selected_recipients(self) -> list[int]:
        return [
            item.data(Qt.ItemDataRole.UserRole) for item in self.recipients.selectedItems()
        ]

    # ------------------------------------------------------------------ файлы

    def dragEnterEvent(self, event) -> None:  # noqa: N802 — Qt
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 — Qt
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls()]
        self._add_files([p for p in paths if p.is_file()])

    def _pick_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Выберите файлы")
        self._add_files([Path(n) for n in names])

    def _add_files(self, paths: list[Path]) -> None:
        for path in paths:
            if path in self.files:
                continue
            self.files.append(path)
            self.file_list.addItem(f"{path.name}   {human_size(path.stat().st_size)}")
        self._update_summary()

    def _remove_file(self) -> None:
        row = self.file_list.currentRow()
        if row >= 0:
            self.file_list.takeItem(row)
            del self.files[row]
            self._update_summary()

    def _update_summary(self) -> None:
        if not self.files:
            self.summary.setText("Перетащите файлы сюда или нажмите «Выбрать файлы»")
            return
        total = sum(p.stat().st_size for p in self.files)
        text = f"Итого {human_size(total)} · файлов: {len(self.files)}"

        # Оценка по фактической скорости прошлых передач, а не по номиналу сети (3.2).
        # Пока передач не было, ничего не выдумываем: неверная оценка хуже её отсутствия.
        eta = self.core.estimate_seconds(total)
        if eta is not None:
            text += f" · примерно {human_eta(eta).lstrip('~')}"
        self.summary.setText(text)

    # ------------------------------------------------------------------ отправка

    def _save_draft(self) -> None:
        """Черновик сохраняется локально и работает без связи с сервером."""
        self.draft_id = self.core.save_draft(
            self.selected_recipients(),
            self.subject.text().strip(),
            self.body.toPlainText().strip(),
            self.files,
            draft_id=self.draft_id,
        )
        self.accept()

    def _send(self) -> None:
        recipients = self.selected_recipients()
        if not recipients:
            QMessageBox.information(self, "FilePost", "Выберите хотя бы одного получателя.")
            return
        if not self.files:
            QMessageBox.information(self, "FilePost", "Добавьте хотя бы один файл.")
            return
        try:
            self.core.compose(
                recipients,
                self.subject.text().strip(),
                self.body.toPlainText().strip(),
                self.files,
                draft_id=self.draft_id,
            )
        except ApiError as exc:
            # Без связи письмо не потеряется: предлагаем убрать его в черновики.
            answer = QMessageBox.question(
                self,
                "FilePost",
                f"{exc.message}\n\nСохранить письмо в черновиках?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._save_draft()
            return
        self.accept()
