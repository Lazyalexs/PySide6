from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filepost.app import create_app  # noqa: E402
from filepost.auth import create_enrollment_code  # noqa: E402
from filepost.config import Config, StorageConfig  # noqa: E402
from filepost.db import Database  # noqa: E402


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(root=tmp_path)
    cfg.storage = StorageConfig(
        path=str(tmp_path / "storage"),
        tmp_path=str(tmp_path / "tmp"),
        chunk_size_mb=1,
        min_free_space_gb=0,
        max_file_size_gb=1,
    )
    # По умолчанию бэкап уходит на E:\ — на машине без такого диска тесты
    # ломались бы о реальную файловую систему вместо проверки логики.
    cfg.backup.path = str(tmp_path / "backups")
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def db(cfg: Config) -> Database:
    db = Database(cfg.db_path)
    db.init_schema()
    return db


@pytest.fixture
def client(cfg: Config, db: Database) -> TestClient:
    return TestClient(create_app(cfg, db))


class StationClient:
    """Обёртка вокруг TestClient: держит токен и не заставляет его подставлять руками."""

    def __init__(self, client: TestClient, station_id: int, secret: str, name: str) -> None:
        self.http = client
        self.station_id = station_id
        self.secret = secret
        self.name = name
        self.token = ""
        self.login()

    def login(self) -> None:
        r = self.http.post(
            "/api/auth/token",
            json={"station_id": self.station_id, "secret": self.secret,
                  "client_version": "1.0.0"},
        )
        assert r.status_code == 200, r.text
        self.token = r.json()["token"]

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _kw(self, kw: dict) -> dict:
        kw["headers"] = {**self.headers, **(kw.get("headers") or {})}
        return kw

    def get(self, url: str, **kw):
        return self.http.get(url, **self._kw(kw))

    def post(self, url: str, **kw):
        return self.http.post(url, **self._kw(kw))

    def put(self, url: str, **kw):
        return self.http.put(url, **self._kw(kw))

    def patch(self, url: str, **kw):
        return self.http.patch(url, **self._kw(kw))

    def delete(self, url: str, **kw):
        return self.http.delete(url, **self._kw(kw))

    def send_file(self, to: list[int], data: bytes, name: str = "file.bin",
                  subject: str = "тема") -> tuple[int, int]:
        """Полный цикл отправки: создать → init → чанки → commit → send."""
        r = self.post("/api/messages",
                      json={"subject": subject, "body": "", "recipients": to})
        assert r.status_code == 200, r.text
        message_id = r.json()["message_id"]

        digest = hashlib.sha256(data).hexdigest()
        r = self.post(
            f"/api/messages/{message_id}/attachments/init",
            json={"name": name, "size": len(data), "sha256": digest},
        )
        assert r.status_code == 200, r.text
        upload = r.json()
        chunk_size = upload["chunk_size"]

        for index, start in enumerate(range(0, max(len(data), 1), chunk_size)):
            piece = data[start:start + chunk_size]
            r = self.put(f"/api/uploads/{upload['upload_id']}/chunk/{index}", content=piece)
            assert r.status_code == 200, r.text

        r = self.post(f"/api/uploads/{upload['upload_id']}/commit")
        assert r.status_code == 202, r.text

        r = self.post(f"/api/messages/{message_id}/send")
        assert r.status_code == 200, r.text
        return message_id, upload["attachment_id"]


@pytest.fixture
def make_station(client: TestClient, cfg: Config, db: Database):
    def _make(name: str, *, is_admin: bool = False) -> StationClient:
        code = create_enrollment_code(db, cfg, is_admin=is_admin)["enrollment_code"]
        r = client.post(
            "/api/stations/register",
            json={
                "enrollment_code": code,
                "display_name": name,
                "machine_name": name.upper().replace(" ", "-"),
                "client_version": "1.0.0",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        return StationClient(client, data["station_id"], data["secret"], name)

    return _make


@pytest.fixture
def buh(make_station) -> StationClient:
    return make_station("Бухгалтерия, окно 2", is_admin=True)


@pytest.fixture
def sklad(make_station) -> StationClient:
    return make_station("Склад")
