"""Приём чанков, резервирование места, сборка и раздача файлов. Разделы 2.7, 2.12."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Iterator

from .config import Config
from .db import Database
from .journal import audit
from .util import age_seconds, free_space, utcnow

READ_BLOCK = 1024 * 1024


class StorageError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class NoSpace(StorageError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 507)


# --------------------------------------------------------------------------- места


def reserved_bytes(db: Database) -> int:
    """Сколько места уже обещано незавершённым заливкам.

    Нигде не хранится отдельно — вычисляется из активных сессий, поэтому переживает
    перезапуск службы без отдельного механизма восстановления (2.12).
    """
    rows = db.query(
        "SELECT total_size, chunk_size, received_chunks FROM upload_sessions"
        " WHERE state = 'active'"
    )
    total = 0
    for row in rows:
        received = len(json.loads(row["received_chunks"]))
        uploaded = min(received * row["chunk_size"], row["total_size"])
        total += max(row["total_size"] - uploaded, 0)
    return total


def check_space(db: Database, cfg: Config, size: int) -> None:
    """free − Σ(обещанного) − size ≥ min_free_space (2.7).

    Без вычитания резерва проверка гоночная: семь станций по две заливки до 20 ГБ
    получат «место есть» одновременно и вместе забьют диск.
    """
    if size > cfg.storage.max_file_size:
        raise StorageError(
            f"Файл больше допустимых {cfg.storage.max_file_size_gb} ГБ", 413
        )
    available = free_space(cfg.storage_path) - reserved_bytes(db) - size
    if available < cfg.storage.min_free_space:
        raise NoSpace("На сервере нет места с учётом незавершённых загрузок")


class DownloadSlots:
    """Учёт идущих скачиваний по станциям. Раздел 2.6, `max_parallel_downloads_per_user`.

    Без этого пятеро получателей одного письма разом тянут файл на 5 ГБ, упираются
    в диск, и обычная отправка в этот момент встаёт.

    Счётчик держится в памяти намеренно: он описывает соединения, живущие прямо
    сейчас, и после перезапуска службы их не существует — восстанавливать нечего.
    """

    def __init__(self) -> None:
        self._active: dict[int, int] = {}
        self._lock = threading.Lock()

    def count(self, station_id: int) -> int:
        with self._lock:
            return self._active.get(station_id, 0)

    def acquire(self, station_id: int, limit: int) -> bool:
        with self._lock:
            current = self._active.get(station_id, 0)
            if limit > 0 and current >= limit:
                return False
            self._active[station_id] = current + 1
            return True

    def release(self, station_id: int) -> None:
        with self._lock:
            current = self._active.get(station_id, 0) - 1
            if current > 0:
                self._active[station_id] = current
            else:
                self._active.pop(station_id, None)


def release_stale_reservations(db: Database, cfg: Config) -> int:
    """Сессия без чанков дольше reservation_idle_hours перестаёт держать резерв.

    Таймера здесь два и это не дублирование: резерв отпускается через 2 часа, а сами
    чанки в tmp\\ живут 48 — на случай, если станция вернётся и продолжит докачку.
    """
    limit = cfg.cleanup.reservation_idle_hours * 3600
    freed = 0
    for row in db.query("SELECT id, updated_at FROM upload_sessions WHERE state = 'active'"):
        age = age_seconds(row["updated_at"])
        if age is not None and age > limit:
            db.execute("UPDATE upload_sessions SET state = 'stale' WHERE id = ?", (row["id"],))
            freed += 1
    return freed


# --------------------------------------------------------------------------- пути


def session_dir(cfg: Config, upload_id: str) -> Path:
    return cfg.tmp_path / upload_id


def chunk_path(cfg: Config, upload_id: str, index: int) -> Path:
    return session_dir(cfg, upload_id) / f"chunk_{index:04d}"


def new_storage_path(cfg: Config) -> tuple[str, Path]:
    """Имя на диске — UUID: оригинальное имя живёт только в БД, path traversal невозможен."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    rel = Path(f"{now:%Y}") / f"{now:%m}" / f"{uuid.uuid4()}.bin"
    full = cfg.storage_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    return str(rel).replace("/", "\\"), full


def resolve(cfg: Config, storage_path: str) -> Path:
    return cfg.storage_path / Path(storage_path.replace("\\", "/"))


# --------------------------------------------------------------------------- чанки


def write_chunk(db: Database, cfg: Config, upload_id: str, index: int, stream: Iterator[bytes]) -> int:
    session = db.one("SELECT * FROM upload_sessions WHERE id = ?", (upload_id,))
    if session is None:
        raise StorageError("Сессия загрузки не найдена", 404)
    if session["state"] == "committed":
        raise StorageError("Загрузка уже завершена", 409)

    total_chunks = max(1, -(-session["total_size"] // session["chunk_size"]))
    if not 0 <= index < total_chunks:
        raise StorageError("Номер чанка вне диапазона", 400)

    path = chunk_path(cfg, upload_id, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Пишем через временный файл: оборванный PUT не должен оставить короткий чанк,
    # который потом примут за целый.
    partial = path.with_suffix(".part")
    written = 0
    with partial.open("wb") as fh:
        for block in stream:
            fh.write(block)
            written += len(block)
    partial.replace(path)

    received = set(json.loads(session["received_chunks"]))
    received.add(index)
    db.execute(
        "UPDATE upload_sessions SET received_chunks = ?, updated_at = ?, state = 'active'"
        " WHERE id = ?",
        (json.dumps(sorted(received)), utcnow(), upload_id),
    )
    return written


def own_session(db: Database, upload_id: str, station_id: int):
    """Сессия загрузки, принадлежащая именно этой станции.

    Одна точка проверки на все endpoint'ы сессии. Пока каждый проверял сам,
    у /status проверки просто не оказалось, и по upload_id было видно, что и
    какого размера заливает соседний кабинет.
    """
    session = db.one("SELECT * FROM upload_sessions WHERE id = ?", (upload_id,))
    if session is None:
        raise StorageError("Сессия загрузки не найдена", 404)
    if session["station_id"] != station_id:
        raise StorageError("Чужая сессия загрузки", 403)
    return session


def session_status(session) -> dict:
    """Что докачивать: строка сессии приходит из own_session, а не по id.

    Так проверку владельца нельзя обойти, забыв её вызвать.
    """
    upload_id = session["id"]
    total_chunks = max(1, -(-session["total_size"] // session["chunk_size"]))
    return {
        "upload_id": upload_id,
        "received_chunks": json.loads(session["received_chunks"]),
        "total_chunks": total_chunks,
        "chunk_size": session["chunk_size"],
        "state": session["state"],
    }


# --------------------------------------------------------------------------- сборка


def assemble(db: Database, cfg: Config, upload_id: str) -> None:
    """Собрать файл, сверить sha256, перенести в storage.

    Порядок всегда один: сначала диск, потом БД (2.12). Запись ready, указывающая
    в пустоту, хуже лишнего файла на диске — первое это отправитель, уверенный
    в доставке, второе находится сверкой.

    Операция идемпотентна: чанки лежат в tmp\\ до самого конца, поэтому повтор после
    падения — просто повтор, а не восстановление по обломкам.
    """
    session = db.one("SELECT * FROM upload_sessions WHERE id = ?", (upload_id,))
    if session is None:
        raise StorageError("Сессия загрузки не найдена", 404)

    attachment = db.one("SELECT * FROM attachments WHERE id = ?", (session["attachment_id"],))
    if attachment is None:
        raise StorageError("Вложение не найдено", 404)
    if attachment["state"] == "ready":
        return
    db.execute(
        "UPDATE attachments SET state = 'assembling' WHERE id = ? AND state = 'uploading'",
        (attachment["id"],),
    )

    total_chunks = max(1, -(-session["total_size"] // session["chunk_size"]))
    received = set(json.loads(session["received_chunks"]))
    missing = sorted(set(range(total_chunks)) - received)
    if missing:
        _fail(db, attachment["id"], f"не получены чанки: {missing[:10]}")
        raise StorageError(f"Не получены все чанки, отсутствуют: {missing[:10]}", 409)

    # Промежуточный файл получает отдельное имя и удаляется перед новой попыткой,
    # чтобы недособранный кусок нельзя было принять за готовый.
    staging = session_dir(cfg, upload_id) / "assembled.tmp"
    if staging.exists():
        staging.unlink()

    digest = hashlib.sha256()
    size = 0
    with staging.open("wb") as out:
        for index in range(total_chunks):
            part = chunk_path(cfg, upload_id, index)
            if not part.exists():
                _fail(db, attachment["id"], f"чанк {index} пропал с диска")
                raise StorageError(f"Чанк {index} отсутствует на диске", 409)
            with part.open("rb") as fh:
                while block := fh.read(READ_BLOCK):
                    out.write(block)
                    digest.update(block)
                    size += len(block)

    if size != attachment["size"] or digest.hexdigest() != attachment["sha256"]:
        staging.unlink(missing_ok=True)
        _fail(db, attachment["id"], "sha256 не совпал")
        raise StorageError("Контрольная сумма не совпала", 409)

    rel, full = new_storage_path(cfg)
    os.replace(staging, full)          # 1. диск
    db.execute(                         # 2. БД
        "UPDATE attachments SET storage_path = ?, state = 'ready' WHERE id = ?",
        (rel, attachment["id"]),
    )
    db.execute("UPDATE upload_sessions SET state = 'committed' WHERE id = ?", (upload_id,))
    _cleanup_session_dir(cfg, upload_id)
    audit(db, session["station_id"], "attachment.ready", attachment["id"], size=size)


def _fail(db: Database, attachment_id: int, reason: str) -> None:
    db.execute("UPDATE attachments SET state = 'failed' WHERE id = ?", (attachment_id,))
    audit(db, None, "attachment.failed", attachment_id, reason=reason)


def _cleanup_session_dir(cfg: Config, upload_id: str) -> None:
    directory = session_dir(cfg, upload_id)
    if not directory.exists():
        return
    for item in directory.iterdir():
        item.unlink(missing_ok=True)
    directory.rmdir()


# --------------------------------------------------------------------------- раздача


def iter_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    """Отдача куска файла потоком: файл целиком в память не поднимается никогда."""
    remaining = end - start + 1
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            block = fh.read(min(READ_BLOCK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Разбор `Range: bytes=start-end`. Возвращает None, если заголовка нет.

    Заголовок приходит снаружи, и мусор в нём — это 416, а не 500: заголовки
    сочиняет не только наш клиент, и падать на них служба не должна.
    """
    if not header or not header.startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        else:
            # bytes=-N — последние N байт
            length = int(end_s or 0)
            start = max(size - length, 0)
            end = size - 1
    except ValueError:
        raise StorageError("Некорректный заголовок Range", 416) from None
    if start >= size or start > end:
        raise StorageError("Диапазон вне размера файла", 416)
    return start, min(end, size - 1)
