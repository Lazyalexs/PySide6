"""Загрузка config.toml. Значения по умолчанию совпадают с разделом 2.6 архитектуры."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

MB = 1024 * 1024
GB = 1024 * MB


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    token_ttl_hours: int = 12
    presence_timeout_sec: int = 45
    min_client_version: str = "1.0.0"
    discovery_enabled: bool = False
    discovery_port: int = 8081
    enrollment_ttl_hours: int = 24


@dataclass
class StorageConfig:
    path: str = "D:\\FilePost\\storage"
    tmp_path: str = "D:\\FilePost\\tmp"
    chunk_size_mb: int = 16
    min_free_space_gb: int = 50
    max_file_size_gb: int = 20

    @property
    def chunk_size(self) -> int:
        return self.chunk_size_mb * MB

    @property
    def min_free_space(self) -> int:
        return self.min_free_space_gb * GB

    @property
    def max_file_size(self) -> int:
        return self.max_file_size_gb * GB


@dataclass
class RetentionConfig:
    enabled: bool = False
    delete_after_download_days: int = 7
    delete_never_downloaded_days: int = 30
    notify_sender_before_days: int = 2
    delete_orphaned: bool = False


@dataclass
class CleanupConfig:
    abandoned_uploads_hours: int = 48
    reservation_idle_hours: int = 2
    events_retention_days: int = 30


@dataclass
class BackupConfig:
    enabled: bool = True
    path: str = "E:\\Backup\\FilePost"
    time: str = "03:00"
    keep_copies: int = 14


@dataclass
class LimitsConfig:
    max_parallel_uploads_per_user: int = 2
    max_parallel_downloads_per_user: int = 2
    max_recipients_per_message: int = 20
    max_attachments_per_message: int = 50


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)

    #: каталог, в котором лежит config.toml; db/ и logs/ считаются от него
    root: Path = field(default_factory=lambda: Path.cwd())

    @property
    def storage_path(self) -> Path:
        return Path(self.storage.path)

    @property
    def tmp_path(self) -> Path:
        return Path(self.storage.tmp_path)

    @property
    def db_path(self) -> Path:
        return self.root / "db" / "filepost.db"

    @property
    def logs_path(self) -> Path:
        return self.root / "logs"

    def ensure_dirs(self) -> None:
        for p in (self.storage_path, self.tmp_path, self.db_path.parent, self.logs_path):
            p.mkdir(parents=True, exist_ok=True)


def _fill(target: Any, raw: dict[str, Any]) -> None:
    known = {f.name for f in fields(target)}
    for key, value in raw.items():
        if key in known:
            setattr(target, key, value)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    cfg = Config(root=path.parent.resolve())
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        for section in fields(Config):
            if section.name == "root":
                continue
            value = getattr(cfg, section.name)
            if is_dataclass(value) and section.name in raw:
                _fill(value, raw[section.name])
    return cfg


DEFAULT_CONFIG_TOML = """\
[server]
host = "0.0.0.0"
port = 8080
token_ttl_hours = 12
presence_timeout_sec = 45
min_client_version = "1.0.0"
discovery_enabled = false
discovery_port = 8081

[storage]
path = "D:\\\\FilePost\\\\storage"
tmp_path = "D:\\\\FilePost\\\\tmp"
chunk_size_mb = 16
min_free_space_gb = 50
max_file_size_gb = 20

[retention]
enabled = false
delete_after_download_days = 7
delete_never_downloaded_days = 30
notify_sender_before_days = 2
delete_orphaned = false

[cleanup]
abandoned_uploads_hours = 48
reservation_idle_hours = 2
events_retention_days = 30

[backup]
enabled = true
path = "E:\\\\Backup\\\\FilePost"
time = "03:00"
keep_copies = 14

[limits]
max_parallel_uploads_per_user = 2
max_parallel_downloads_per_user = 2
max_recipients_per_message = 20
max_attachments_per_message = 50
"""
