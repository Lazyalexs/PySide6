"""UDP-ответчик для автопоиска сервера. Раздел 2.9.

Запасной путь, по умолчанию выключен с обеих сторон. Ответ ничем не подписан —
любая машина в сети может ответить первой, поэтому discovery включается только
когда адреса нет вовсе, а найденный адрес клиент сразу фиксирует в конфиге.
"""

from __future__ import annotations

import logging
import socket
import threading

from .config import Config

log = logging.getLogger("filepost.discovery")

REQUEST = b"FILEPOST_DISCOVER_V1"
RESPONSE_PREFIX = b"FILEPOST_SERVER_V1 "


class DiscoveryResponder:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not self.cfg.server.discovery_enabled or self._thread is not None:
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        self._sock.bind(("0.0.0.0", self.cfg.server.discovery_port))
        self._thread = threading.Thread(target=self._loop, name="discovery", daemon=True)
        self._thread.start()
        log.info("UDP-автопоиск слушает порт %s", self.cfg.server.discovery_port)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() != REQUEST:
                continue
            url = f"http://{self._local_ip(addr[0])}:{self.cfg.server.port}"
            try:
                self._sock.sendto(RESPONSE_PREFIX + url.encode(), addr)
            except OSError:
                log.debug("не удалось ответить на discovery от %s", addr)

    def _local_ip(self, peer: str) -> str:
        """Адрес, по которому именно этот клиент сможет достучаться до сервера."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((peer, 1))
            return probe.getsockname()[0]
        except OSError:
            return socket.gethostbyname(socket.gethostname())
        finally:
            probe.close()


def discover(
    port: int = 8081, timeout: float = 2.0, targets: list[str] | None = None
) -> str | None:
    """Клиентская сторона: широковещательный запрос, первый ответивший выигрывает.

    Адресов несколько намеренно: на части конфигураций Windows `255.255.255.255`
    не уходит, а широковещательный адрес подсети проходит.
    """
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
    """Грубая оценка broadcast-адреса по локальному IP, в расчёте на /24."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        local = probe.getsockname()[0]
        probe.close()
        parts = local.split(".")
        return [".".join(parts[:3] + ["255"])]
    except OSError:
        return []
