"""Мелочи, общие для всех модулей: время, размеры, свободное место."""

from __future__ import annotations

import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Всё время в системе — UTC ISO-8601 с суффиксом Z. Локальное время появляется
#: только при отображении в клиенте (см. замечание про часовые пояса в 2.x).
ISO = "%Y-%m-%dT%H:%M:%SZ"


def now() -> datetime:
    return datetime.now(timezone.utc)


def utcnow() -> str:
    return now().strftime(ISO)


def in_hours(hours: float) -> str:
    return (now() + timedelta(hours=hours)).strftime(ISO)


def parse(ts: str | None) -> datetime | None:
    """Неразбираемая метка времени — это None, а не исключение.

    Строки приходят из БД, которую могли восстановить из копии или поправить
    руками. Считать такую метку отсутствующей безопасно: expired() ответит
    «просрочено», а age_seconds() — «возраст неизвестен».
    """
    if not ts:
        return None
    try:
        return datetime.strptime(ts, ISO).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def expired(ts: str | None) -> bool:
    dt = parse(ts)
    return dt is None or dt <= now()


def age_seconds(ts: str | None) -> float | None:
    dt = parse(ts)
    return None if dt is None else (now() - dt).total_seconds()


def free_space(path: Path) -> int:
    return shutil.disk_usage(path).free


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def new_enrollment_code() -> str:
    """Код вида 4F2A-91C7-DE38 — его вводят руками, поэтому без похожих символов."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "-".join(groups)


def human_size(n: int) -> str:
    for unit, limit in (("ТБ", 1 << 40), ("ГБ", 1 << 30), ("МБ", 1 << 20), ("КБ", 1 << 10)):
        if n >= limit:
            return f"{n / limit:.1f} {unit}".replace(".", ",")
    return f"{n} Б"
