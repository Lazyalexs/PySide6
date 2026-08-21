"""Ядро клиента проверяется против настоящего сервера, поднятого в потоке.

Окна не создаются вообще — ровно то, ради чего слои разведены однонаправленно (3.1).
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "server"))

from filepost.app import create_app  # noqa: E402
from filepost.auth import create_enrollment_code  # noqa: E402
from filepost.config import Config as ServerConfig  # noqa: E402
from filepost.config import StorageConfig  # noqa: E402
from filepost.db import Database  # noqa: E402
from filepost_client.core import Core  # noqa: E402
from filepost_client.settings import Settings  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerFixture:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "server"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg = ServerConfig(root=self.dir)
        self.cfg.storage = StorageConfig(
            path=str(self.dir / "storage"),
            tmp_path=str(self.dir / "tmp"),
            chunk_size_mb=1,
            min_free_space_gb=0,
            max_file_size_gb=1,
        )
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.db.init_schema()
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"

        app = create_app(self.cfg, self.db)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("сервер не поднялся")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def enrollment_code(self, is_admin: bool = False) -> str:
        return create_enrollment_code(self.db, self.cfg, is_admin=is_admin)["enrollment_code"]


@pytest.fixture
def server(tmp_path: Path):
    fixture = ServerFixture(tmp_path)
    fixture.start()
    yield fixture
    fixture.stop()


@pytest.fixture
def make_client(server, tmp_path: Path):
    created: list[Core] = []

    def _make(name: str, *, is_admin: bool = False, start: bool = False) -> Core:
        root = tmp_path / f"client-{name}"
        settings = Settings(root=root)
        settings.server.url = server.url
        settings.server.poll_interval_sec = 5
        settings.prefs.downloads_dir = str(root / "downloads")
        core = Core(settings)
        core.register(server.enrollment_code(is_admin=is_admin), name)
        assert core.connect(), core.last_error
        core.refresh_all()
        if start:
            core.start()
        created.append(core)
        return core

    yield _make
    for core in created:
        core.stop()


@pytest.fixture
def buh(make_client):
    return make_client("Бухгалтерия, окно 2", is_admin=True)


@pytest.fixture
def sklad(make_client):
    return make_client("Склад")


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.1):
    """Ждать условия вместо фиксированных пауз — тесты не должны быть флаки."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None
