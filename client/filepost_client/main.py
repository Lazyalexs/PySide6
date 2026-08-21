"""Точка входа клиента."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from .core import Core
from .settings import Settings, default_root


def setup_logging(settings: Settings) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        settings.logs_dir / "client.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("filepost")
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def setup_autostart(enabled: bool) -> None:
    """Автозапуск через ключ реестра Run (раздел 4). На не-Windows — no-op."""
    if sys.platform != "win32":
        return
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(
                key, "FilePost", 0, winreg.REG_SZ, f'"{sys.executable}" --minimized'
            )
        else:
            try:
                winreg.DeleteValue(key, "FilePost")
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filepost", description="FilePost — клиент")
    parser.add_argument("--data-dir", default=None, help="каталог данных клиента")
    parser.add_argument("--minimized", action="store_true", help="стартовать свёрнутым")
    args = parser.parse_args(argv)

    settings = Settings(root=Path(args.data_dir) if args.data_dir else default_root()).load()
    settings.ensure_dirs()
    setup_logging(settings)

    from PySide6.QtWidgets import QApplication, QMessageBox

    from .ui.main_window import MainWindow
    from .ui.setup import SetupDialog

    app = QApplication(sys.argv)
    app.setApplicationName("FilePost")
    app.setQuitOnLastWindowClosed(False)  # окно закрывается в трей, а не выходит

    core = Core(settings)

    # Единственное место, где человек что-то вводит, и только один раз.
    if not settings.registered:
        if not SetupDialog(core).exec():
            return 1
    elif not core.connect():
        # Не блокируемся: окно откроется на локальной БД с баннером (3.6).
        QMessageBox.warning(
            None,
            "FilePost",
            f"{core.last_error}\n\nПрограмма запустится в автономном режиме: "
            f"письма читаются из локальной копии, исходящие уйдут при появлении связи.",
        )

    setup_autostart(settings.prefs.autostart)
    core.refresh_health()
    core.refresh_all()
    core.start()

    window = MainWindow(core)
    if args.minimized or settings.prefs.start_minimized:
        window.hide()
    else:
        window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
