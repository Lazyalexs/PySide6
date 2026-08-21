"""Первый запуск: единственное окно, где человек что-то вводит. Раздел 2.10.

Код регистрации выдаёт администратор. Дальше клиент хранит выданный ключ и входит
сам — формы входа в системе нет.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from ..api import ApiError
from ..core import Core
from ..util import machine_name


class SetupDialog(QDialog):
    def __init__(self, core: Core, parent=None) -> None:
        super().__init__(parent)
        self.core = core
        self.setWindowTitle("FilePost — первоначальная настройка")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Эта станция ещё не зарегистрирована.\n"
            "Введите код регистрации, который выдал администратор."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.url = QLineEdit(core.settings.server.url or "")
        self.url.setPlaceholderText("filepost-srv:8080")

        self.code = QLineEdit()
        self.code.setPlaceholderText("XXXX-XXXX-XXXX")
        self.code.setInputMask(">NNNN-NNNN-NNNN;_")

        self.name = QLineEdit(machine_name())

        form.addRow("Адрес сервера:", self.url)
        form.addRow("Код регистрации:", self.code)
        form.addRow("Название станции:", self.name)
        layout.addLayout(form)

        hint = QLabel(
            "Название станции — это то, что увидят остальные в списке получателей. "
            "Например: «Бухгалтерия, окно 2»."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        layout.addWidget(hint)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Зарегистрировать")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._register)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _register(self) -> None:
        code = self.code.text().strip()
        if len(code.replace("-", "").replace("_", "")) != 12:
            self.status.setText("Код должен состоять из 12 символов.")
            return
        self.status.setText("Регистрация…")
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            self.core.register(code, self.name.text().strip(), self.url.text().strip())
        except ApiError as exc:
            self.status.setText(exc.message)
            return
        finally:
            self.unsetCursor()

        if not self.core.connect():
            QMessageBox.warning(self, "FilePost", self.core.last_error)
            return
        self.accept()
