"""Точка входа для PyInstaller.

Нужна отдельно от `filepost_client/main.py`: PyInstaller запускает скрипт как
`__main__` без пакетного контекста, и относительные импорты внутри main.py
(`from .core import Core`) там падают с ImportError. Здесь импорт абсолютный,
поэтому пакет загружается штатно.

Запуск из исходников по-прежнему работает через `python -m filepost_client.main`.
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    # Обязательно до любых импортов, порождающих процессы: без этого замороженное
    # приложение на Windows перезапускает само себя вместо создания дочернего процесса.
    multiprocessing.freeze_support()

    from filepost_client.main import main

    sys.exit(main())
