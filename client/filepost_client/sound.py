"""Звук уведомлений. Настройка «Звук» в разделе 3.7.

На Windows играем через `winsound` из стандартной библиотеки: он умеет WAV,
работает асинхронно и не тянет в сборку QtMultimedia, который раздувает .exe
в несколько раз ради одного короткого сигнала.

Вне Windows звук не воспроизводится — это среда разработки, не цель поставки.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("filepost.sound")

RESOURCES = Path(__file__).parent / "resources"
NOTIFY_WAV = "notify.wav"


def resource_path(name: str = NOTIFY_WAV) -> Path:
    """Путь к ресурсу с учётом сборки PyInstaller.

    В собранном .exe ресурсы распаковываются во временный каталог, на который
    указывает sys._MEIPASS, а не лежат рядом с модулем.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        bundled = Path(base) / "resources" / name
        if bundled.exists():
            return bundled
    return RESOURCES / name


def available() -> bool:
    return sys.platform == "win32" and resource_path().exists()


def play_notification(enabled: bool = True) -> bool:
    """Проиграть звук уведомления. Возвращает True, если звук ушёл на воспроизведение.

    Никогда не бросает исключений и не блокирует поток: уведомление — не та
    операция, ради которой стоит ронять интерфейс или подвешивать его на три
    секунды проигрывания.
    """
    if not enabled:
        return False

    path = resource_path()
    if not path.exists():
        log.debug("файл звука не найден: %s", path)
        return False

    if sys.platform != "win32":
        log.debug("воспроизведение звука доступно только на Windows")
        return False

    try:
        import winsound

        winsound.PlaySound(
            str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        )
        return True
    except Exception:  # noqa: BLE001 — звук не критичен, молча продолжаем
        log.debug("не удалось проиграть звук", exc_info=True)
        return False
