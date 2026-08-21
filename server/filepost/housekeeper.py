"""Фоновый планировщик: уборка мусора, бэкап, опциональная очистка по сроку.

Разделы 2.6, 2.13. Автоочистка пользовательских файлов выключена по умолчанию —
ничего не удаляется само. Уборка tmp\\ и обрезка журнала событий работают всегда:
это не пользовательские данные.
"""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config
from .db import Database
from .journal import audit
from .messages import find_orphaned
from .storage import release_stale_reservations, resolve
from .util import age_seconds, free_space, human_size, now, utcnow

log = logging.getLogger("filepost.housekeeper")

#: как часто просыпается фоновый цикл
TICK_SECONDS = 300


@dataclass
class SweepReport:
    stale_reservations: int = 0
    abandoned_uploads: int = 0
    events_trimmed: int = 0
    orphaned_marked: list[int] = field(default_factory=list)
    retention_deleted: list[int] = field(default_factory=list)
    backup_path: str | None = None
    low_space: bool = False

    def as_dict(self) -> dict:
        return {
            "stale_reservations": self.stale_reservations,
            "abandoned_uploads": self.abandoned_uploads,
            "events_trimmed": self.events_trimmed,
            "orphaned_marked": self.orphaned_marked,
            "retention_deleted": self.retention_deleted,
            "backup_path": self.backup_path,
            "low_space": self.low_space,
        }

    def is_empty(self) -> bool:
        return not (
            self.stale_reservations
            or self.abandoned_uploads
            or self.events_trimmed
            or self.orphaned_marked
            or self.retention_deleted
            or self.backup_path
            or self.low_space
        )


# --------------------------------------------------------------------------- задачи


def cleanup_abandoned_uploads(db: Database, cfg: Config) -> int:
    """Уборка недокачанного мусора из tmp\\. Работает всегда — это не пользовательские данные.

    Таймер отдельный от резерва места: резерв отпускается через reservation_idle_hours,
    а сами чанки живут abandoned_uploads_hours, чтобы станция могла вернуться и докачать.
    """
    limit = cfg.cleanup.abandoned_uploads_hours * 3600
    removed = 0
    for row in db.query(
        "SELECT id, attachment_id, updated_at FROM upload_sessions WHERE state != 'committed'"
    ):
        age = age_seconds(row["updated_at"])
        if age is None or age <= limit:
            continue
        directory = cfg.tmp_path / row["id"]
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        db.execute("DELETE FROM upload_sessions WHERE id = ?", (row["id"],))
        db.execute(
            "UPDATE attachments SET state = 'failed' WHERE id = ? AND state = 'uploading'",
            (row["attachment_id"],),
        )
        removed += 1

    # Каталоги в tmp\, которым не соответствует ни одна сессия (например, остались
    # после восстановления БД из копии).
    if cfg.tmp_path.exists():
        known = {r["id"] for r in db.query("SELECT id FROM upload_sessions")}
        for directory in cfg.tmp_path.iterdir():
            if directory.is_dir() and directory.name not in known:
                age = (now().timestamp() - directory.stat().st_mtime)
                if age > limit:
                    shutil.rmtree(directory, ignore_errors=True)
                    removed += 1
    return removed


def trim_events(db: Database, cfg: Config) -> int:
    """Обрезка журнала событий. На пользовательские файлы не влияет.

    Станция, чей курсор оказался ниже остатка, получит resync_required и перечитает
    входящие целиком — поэтому обрезать безопасно.
    """
    cutoff = cfg.cleanup.events_retention_days * 86400
    victims = [
        r["id"]
        for r in db.query("SELECT id, created_at FROM events")
        if (age := age_seconds(r["created_at"])) is not None and age > cutoff
    ]
    if not victims:
        return 0
    db.execute(
        f"DELETE FROM events WHERE id IN ({','.join('?' * len(victims))})", victims
    )
    return len(victims)


def mark_orphaned(db: Database) -> list[int]:
    """Пометить вложения, которые скрыли у себя все участники (2.3)."""
    ids = find_orphaned(db)
    if ids:
        db.execute(
            f"UPDATE attachments SET state = 'orphaned' WHERE id IN "
            f"({','.join('?' * len(ids))})",
            ids,
        )
    return ids


def apply_retention(db: Database, cfg: Config) -> list[int]:
    """Очистка по сроку. ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНА — ничего не удаляется само."""
    if not cfg.retention.enabled:
        return []

    deleted: list[int] = []
    after_download = cfg.retention.delete_after_download_days * 86400
    never = cfg.retention.delete_never_downloaded_days * 86400

    for row in db.query(
        "SELECT a.id, a.storage_path, m.sent_at,"
        "  (SELECT MIN(COALESCE(r.downloaded_at,'')) FROM message_recipients r"
        "    WHERE r.message_id = m.id) AS first_download,"
        "  (SELECT COUNT(*) FROM message_recipients r"
        "    WHERE r.message_id = m.id AND r.downloaded_at IS NULL) AS pending"
        " FROM attachments a JOIN messages m ON m.id = a.message_id"
        " WHERE a.state = 'ready' AND m.status = 'sent'"
    ):
        age_sent = age_seconds(row["sent_at"])
        if age_sent is None:
            continue
        downloaded_by_all = row["pending"] == 0
        expired = (
            downloaded_by_all and age_sent > after_download
        ) or (not downloaded_by_all and age_sent > never)
        if expired:
            _drop_file(db, cfg, row["id"], row["storage_path"], reason="retention")
            deleted.append(row["id"])
    return deleted


def delete_orphaned(db: Database, cfg: Config) -> list[int]:
    if not cfg.retention.delete_orphaned:
        return []
    ids = []
    for row in db.query(
        "SELECT id, storage_path FROM attachments WHERE state = 'orphaned'"
    ):
        _drop_file(db, cfg, row["id"], row["storage_path"], reason="orphaned")
        ids.append(row["id"])
    return ids


def _drop_file(db: Database, cfg: Config, attachment_id: int, storage_path: str | None,
               *, reason: str) -> None:
    """Содержимое пропадает, строка остаётся: история переписки и аудит целы (2.3)."""
    if storage_path:
        resolve(cfg, storage_path).unlink(missing_ok=True)
    db.execute(
        "UPDATE attachments SET state = 'deleted', storage_path = NULL WHERE id = ?",
        (attachment_id,),
    )
    audit(db, None, "attachment.autodelete", attachment_id, reason=reason)


def daily_backup(db: Database, cfg: Config, *, force: bool = False) -> str | None:
    """Ночной бэкап БД. Копировать файл в режиме WAL нельзя (2.13).

    `force` — это «сделать копию сейчас» из CLI или админского раздела: сегодняшняя
    копия перезаписывается. Без него работает расписание: копия делается один раз
    в сутки и только после наступления заданного времени.
    """
    if not cfg.backup.enabled:
        return None

    target_dir = Path(cfg.backup.path)
    stamp = date.today().isoformat()
    target = target_dir / f"filepost-{stamp}.db"

    if not force:
        if target.exists():
            return None
        hour, _, minute = cfg.backup.time.partition(":")
        current = now()
        if (current.hour, current.minute) < (int(hour), int(minute or 0)):
            return None

    db.backup_to(target)
    _rotate_backups(target_dir, cfg.backup.keep_copies)
    audit(db, None, "db.backup", None, path=str(target), size=target.stat().st_size)
    log.info("резервная копия базы: %s (%s)", target, human_size(target.stat().st_size))
    return str(target)


def _rotate_backups(directory: Path, keep: int) -> None:
    copies = sorted(directory.glob("filepost-*.db"))
    for stale in copies[:-keep] if keep > 0 else []:
        stale.unlink(missing_ok=True)


def check_space(db: Database, cfg: Config) -> bool:
    """Предупреждение администратору при падении места ниже порога.

    С выключенными retention и delete_orphaned диск заполняется монотонно, и
    единственный тормоз — администратор, пришедший по этому предупреждению (2.6).
    """
    free = free_space(cfg.storage_path)
    if free >= cfg.storage.min_free_space:
        return False
    orphaned = db.scalar("SELECT COUNT(*) FROM attachments WHERE state = 'orphaned'")
    log.warning(
        "СВОБОДНОЕ МЕСТО НИЖЕ ПОРОГА: %s (порог %s). Ничейных вложений: %s — "
        "их удаление никто не заметит",
        human_size(free),
        human_size(cfg.storage.min_free_space),
        orphaned,
    )
    audit(db, None, "storage.low_space", None, free=free, orphaned=orphaned)
    return True


# --------------------------------------------------------------------------- цикл


def sweep(db: Database, cfg: Config, *, force_backup: bool = False) -> SweepReport:
    report = SweepReport()
    report.stale_reservations = release_stale_reservations(db, cfg)
    report.abandoned_uploads = cleanup_abandoned_uploads(db, cfg)
    report.events_trimmed = trim_events(db, cfg)
    report.orphaned_marked = mark_orphaned(db)
    report.retention_deleted = apply_retention(db, cfg) + delete_orphaned(db, cfg)
    report.backup_path = daily_backup(db, cfg, force=force_backup)
    report.low_space = check_space(db, cfg)

    if not report.is_empty():
        log.info("уборка: %s", report.as_dict())
    return report


class Housekeeper:
    """Фоновый поток. Останавливается по Event, чтобы не висеть при выключении службы."""

    def __init__(self, db: Database, cfg: Config, interval: int = TICK_SECONDS) -> None:
        self.db = db
        self.cfg = cfg
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_report: SweepReport | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="housekeeper", daemon=True)
        self._thread.start()
        log.info("планировщик запущен, интервал %s с", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.last_report = sweep(self.db, self.cfg)
            except Exception:  # noqa: BLE001 — фоновый цикл не должен умирать
                log.exception("ошибка в цикле уборки, продолжаю")
