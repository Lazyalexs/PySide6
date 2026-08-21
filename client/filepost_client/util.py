"""Форматирование для интерфейса. Размеры «1,2 ГБ», а не «1288490188 байт» (3.2)."""

from __future__ import annotations

import socket
from datetime import datetime, timezone

ISO = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime(ISO)


def parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, ISO).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def machine_name() -> str:
    return socket.gethostname()


def human_size(n: int | None) -> str:
    if not n:
        return "0 Б"
    for unit, limit in (("ТБ", 1 << 40), ("ГБ", 1 << 30), ("МБ", 1 << 20), ("КБ", 1 << 10)):
        if n >= limit:
            return f"{n / limit:.1f} {unit}".replace(".", ",")
    return f"{n} Б"


def human_speed(bytes_per_sec: float) -> str:
    return f"{human_size(int(bytes_per_sec))}/с"


def human_eta(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"~{seconds} с"
    if seconds < 3600:
        return f"~{seconds // 60} мин"
    return f"~{seconds // 3600} ч {(seconds % 3600) // 60} мин"


def human_time(ts: str | None) -> str:
    """Время — «вчера, 15:20» и «19.08», а не ISO-8601 (3.2)."""
    dt = parse(ts)
    if dt is None:
        return ""
    local = dt.astimezone()
    today = datetime.now().astimezone().date()
    delta = (today - local.date()).days
    if delta == 0:
        return local.strftime("%H:%M")
    if delta == 1:
        return f"вчера, {local:%H:%M}"
    if delta < 365:
        return local.strftime("%d.%m")
    return local.strftime("%d.%m.%Y")


def human_presence(online: bool, last_seen_at: str | None) -> str:
    """«в сети» означает «клиент запущен и видит сервер», а не «человек за столом» (2.10)."""
    if online:
        return "в сети"
    dt = parse(last_seen_at)
    if dt is None:
        return "не в сети"
    minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if minutes < 60:
        return f"{int(minutes)} мин назад"
    if minutes < 1440:
        return f"{int(minutes // 60)} ч назад"
    return f"не в сети с {dt.astimezone():%d.%m}"
