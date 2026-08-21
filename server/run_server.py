"""Точка входа для PyInstaller: одна команда с подкомандами.

Собранный `filepost-server.exe` умеет то же, что `python -m filepost.cli`:

    filepost-server.exe --config config.toml init
    filepost-server.exe --config config.toml serve
    filepost-server.exe --config config.toml station list

Отдельный файл нужен по той же причине, что и у клиента: PyInstaller запускает
скрипт как `__main__` без пакетного контекста, и относительные импорты падают.
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()

    from filepost.cli import main

    sys.exit(main())
