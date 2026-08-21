"""Настройки клиента. Раздел 3.7.

Делятся на два вида, и это разделение важнее самого списка:
  - параметры развёртывания — задаются при установке, в интерфейсе серые
    с подписью «Задано администратором». Сюда попадает всё, чем можно сломать клиент;
  - пользовательские предпочтения — меняются свободно, на других не влияют.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def default_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "FilePost"
    return Path.home() / ".filepost"


@dataclass
class ServerSettings:
    """Параметры развёртывания: пользователю показываются, но не редактируются."""

    url: str = ""
    fallback: str = ""
    timeout_sec: int = 5
    poll_interval_sec: int = 15
    discovery: bool = False
    discovery_port: int = 8081


@dataclass
class StationSettings:
    """Ключ станции, полученный при регистрации. В интерфейсе не показывается вовсе."""

    station_id: int = 0
    secret: str = ""
    display_name: str = ""
    is_admin: bool = False


@dataclass
class Preferences:
    downloads_dir: str = ""
    auto_download: bool = False
    open_folder_after_save: bool = True
    on_name_clash: str = "rename"  # rename | replace
    parallel_transfers: int = 2
    upload_limit_mbps: int = 0  # 0 — без ограничения
    download_limit_mbps: int = 0
    notify_new_message: bool = True
    notify_transfer_done: bool = True
    sound: bool = False
    autostart: bool = True
    start_minimized: bool = True


#: Что нельзя менять из интерфейса — см. таблицу в 3.7.
LOCKED_SECTIONS = {"server", "station"}


@dataclass
class Settings:
    root: Path = field(default_factory=default_root)
    server: ServerSettings = field(default_factory=ServerSettings)
    station: StationSettings = field(default_factory=StationSettings)
    prefs: Preferences = field(default_factory=Preferences)

    @property
    def config_path(self) -> Path:
        return self.root / "config.ini"

    @property
    def db_path(self) -> Path:
        return self.root / "client.db"

    @property
    def downloads_dir(self) -> Path:
        return Path(self.prefs.downloads_dir) if self.prefs.downloads_dir else self.root / "downloads"

    @property
    def partial_dir(self) -> Path:
        return self.root / "partial"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def registered(self) -> bool:
        return bool(self.station.station_id and self.station.secret)

    def ensure_dirs(self) -> None:
        for path in (self.root, self.downloads_dir, self.partial_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ файл

    def load(self) -> "Settings":
        if not self.config_path.exists():
            return self
        parser = configparser.ConfigParser()
        parser.read(self.config_path, encoding="utf-8")
        for name, target in (
            ("server", self.server),
            ("station", self.station),
            ("prefs", self.prefs),
        ):
            if not parser.has_section(name):
                continue
            for f in fields(target):
                if not parser.has_option(name, f.name):
                    continue
                raw = parser.get(name, f.name)
                if f.type is bool or isinstance(getattr(target, f.name), bool):
                    value: object = parser.getboolean(name, f.name)
                elif isinstance(getattr(target, f.name), int):
                    value = parser.getint(name, f.name)
                else:
                    value = raw
                setattr(target, f.name, value)
        return self

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        parser = configparser.ConfigParser()
        parser["server"] = {k: str(v) for k, v in asdict(self.server).items()}
        parser["station"] = {k: str(v) for k, v in asdict(self.station).items()}
        parser["prefs"] = {k: str(v) for k, v in asdict(self.prefs).items()}
        with self.config_path.open("w", encoding="utf-8") as fh:
            parser.write(fh)

    # ------------------------------------------------------------------ адреса

    def candidate_urls(self) -> list[str]:
        """Порядок разрешения адреса при старте: url → fallback → discovery (2.9)."""
        urls = [u for u in (self.server.url, self.server.fallback) if u]
        return [u if u.startswith("http") else f"http://{u}" for u in urls]

    def remember_url(self, url: str) -> None:
        """Найденный автопоиском адрес сразу фиксируется в конфиге."""
        if url and url != self.server.url:
            self.server.url = url
            self.save()
