"""Процедура восстановления при старте службы и сверка БД с диском. Раздел 2.12.

Опасны не сами сбои, а промежутки между двумя действиями, которые должны были
случиться вместе. Служба прогоняет эту процедуру при каждом старте и только потом
начинает принимать запросы.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .db import Database
from .journal import audit
from .storage import READ_BLOCK, assemble, release_stale_reservations, resolve

log = logging.getLogger("filepost.recovery")


@dataclass
class RecoveryReport:
    reassembled: list[int] = field(default_factory=list)
    confirmed: list[int] = field(default_factory=list)
    marked_missing: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    stale_sessions: int = 0

    def as_dict(self) -> dict:
        return {
            "reassembled": self.reassembled,
            "confirmed": self.confirmed,
            "marked_missing": self.marked_missing,
            "failed": self.failed,
            "stale_sessions": self.stale_sessions,
        }

    def is_empty(self) -> bool:
        return not (self.reassembled or self.confirmed or self.marked_missing or self.failed)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(READ_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def run(db: Database, cfg: Config) -> RecoveryReport:
    report = RecoveryReport()

    # 1. Файл перенесён, БД не обновлена: падение между os.replace и UPDATE.
    #    Проверяется первым: если файл уже в storage, пересобирать нечего.
    for row in db.query(
        "SELECT id, storage_path, size, sha256 FROM attachments"
        " WHERE state IN ('uploading','assembling') AND storage_path IS NOT NULL"
    ):
        path = resolve(cfg, row["storage_path"])
        if not path.exists():
            continue
        if path.stat().st_size == row["size"] and file_sha256(path) == row["sha256"]:
            db.execute("UPDATE attachments SET state = 'ready' WHERE id = ?", (row["id"],))
            report.confirmed.append(row["id"])
        else:
            db.execute("UPDATE attachments SET state = 'failed' WHERE id = ?", (row["id"],))
            report.failed.append(row["id"])

    # 2. Сборка прервана на середине. Признака два: вложение осталось в assembling
    #    (служба умерла во время сборки) либо сессия закоммичена, а вложение нет
    #    (умерла между двумя записями).
    for row in db.query(
        "SELECT s.id AS upload_id, s.attachment_id FROM upload_sessions s"
        " JOIN attachments a ON a.id = s.attachment_id"
        " WHERE a.state = 'assembling'"
        "    OR (s.state = 'committed' AND a.state = 'uploading')"
    ):
        try:
            assemble(db, cfg, row["upload_id"])
            report.reassembled.append(row["attachment_id"])
        except Exception as exc:  # noqa: BLE001 — при старте валиться нельзя
            log.warning("не удалось досбрать вложение %s: %s", row["attachment_id"], exc)
            report.failed.append(row["attachment_id"])

    # 3. Запись ready без файла на диске.
    for row in db.query("SELECT id, storage_path FROM attachments WHERE state = 'ready'"):
        if not row["storage_path"] or not resolve(cfg, row["storage_path"]).exists():
            db.execute("UPDATE attachments SET state = 'missing' WHERE id = ?", (row["id"],))
            report.marked_missing.append(row["id"])

    # 4. Резерв места пересчитывается сам, здесь только отпускаем простаивающие сессии.
    report.stale_sessions = release_stale_reservations(db, cfg)

    if not report.is_empty():
        audit(db, None, "recovery.run", None, **report.as_dict())
        log.info("восстановление после сбоя: %s", report.as_dict())
    return report


@dataclass
class VerifyReport:
    """Сверка БД и диска. Обязательна после восстановления БД из копии (2.13)."""

    missing_files: list[dict] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    checked: int = 0

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "missing_files": self.missing_files,
            "orphan_files": self.orphan_files,
        }


def verify_storage(db: Database, cfg: Config, *, mark: bool = True) -> VerifyReport:
    report = VerifyReport()
    known: set[Path] = set()

    for row in db.query(
        "SELECT id, original_name, storage_path FROM attachments"
        " WHERE state IN ('ready','missing') AND storage_path IS NOT NULL"
    ):
        report.checked += 1
        path = resolve(cfg, row["storage_path"])
        known.add(path.resolve())
        if not path.exists():
            report.missing_files.append(
                {"attachment_id": row["id"], "name": row["original_name"]}
            )
            if mark:
                db.execute(
                    "UPDATE attachments SET state = 'missing' WHERE id = ?", (row["id"],)
                )

    if cfg.storage_path.exists():
        for path in cfg.storage_path.rglob("*.bin"):
            if path.resolve() not in known:
                report.orphan_files.append(str(path))

    audit(db, None, "storage.verify", None, **report.as_dict())
    return report
