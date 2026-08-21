"""Клиентская половина UDP-автопоиска. Раздел 2.9.

Крайняя мера, по умолчанию выключена. Ответ ничем не подписан — любая машина в сети
может ответить первой, поэтому найденный адрес сразу фиксируется в конфиге и дальше
используется напрямую.
"""

from __future__ import annotations

import socket

REQUEST = b"FILEPOST_DISCOVER_V1"
RESPONSE_PREFIX = b"FILEPOST_SERVER_V1 "


def discover(
    port: int = 8081, timeout: float = 2.0, targets: list[str] | None = None
) -> str | None:
    """Адресов несколько намеренно: на части конфигураций Windows `255.255.255.255`
    не уходит, а широковещательный адрес подсети проходит."""
    targets = targets or ["255.255.255.255", *_subnet_broadcasts()]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        for target in targets:
            try:
                sock.sendto(REQUEST, (target, port))
            except OSError:
                continue
        while True:
            try:
                data, _ = sock.recvfrom(1024)
            except (socket.timeout, OSError):
                return None
            if data.startswith(RESPONSE_PREFIX):
                return data[len(RESPONSE_PREFIX):].decode().strip()
    finally:
        sock.close()


def _subnet_broadcasts() -> list[str]:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        local = probe.getsockname()[0]
        probe.close()
        return [".".join(local.split(".")[:3] + ["255"])]
    except OSError:
        return []
