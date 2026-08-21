"""FastAPI-приложение: endpoint'ы раздела 2.4.

Здесь намеренно нет `from __future__ import annotations`: FastAPI разрешает
аннотации через globals модуля, а алиасы Current/Admin определены внутри
create_app и в globals не попадают.
"""

import asyncio
import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import housekeeper as hk
from . import journal, messages as msg, recovery, storage
from .auth import AuthError, Station, authenticate, create_enrollment_code, issue_token
from .auth import register_station, reset_station
from .config import Config
from .db import Database
from .discovery import DiscoveryResponder
from .storage import StorageError
from .util import age_seconds, free_space, human_size, utcnow


def version_tuple(value: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in (value or "0").split("."))
    except ValueError:
        return (0,)

log = logging.getLogger("filepost")


# --------------------------------------------------------------------------- схемы


class RegisterIn(BaseModel):
    enrollment_code: str
    display_name: str = ""
    machine_name: str | None = None
    client_version: str | None = None


class TokenIn(BaseModel):
    station_id: int
    secret: str
    client_version: str | None = None


class RenameIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)


class MessageIn(BaseModel):
    subject: str = ""
    body: str = ""
    recipients: list[int]


class AttachmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)


class StationPatchIn(BaseModel):
    display_name: str | None = None
    is_active: bool | None = None
    is_admin: bool | None = None


# --------------------------------------------------------------------------- приложение


def create_app(cfg: Config, db: Database, *, background: bool = False) -> FastAPI:
    keeper = hk.Housekeeper(db, cfg)
    responder = DiscoveryResponder(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if background:
            keeper.start()
            responder.start()
        yield
        keeper.stop()
        responder.stop()

    app = FastAPI(title="FilePost", version="1.0.0", lifespan=lifespan)
    slots = storage.DownloadSlots()
    app.state.cfg = cfg
    app.state.db = db
    app.state.housekeeper = keeper
    app.state.download_slots = slots

    def current(authorization: Annotated[str | None, Header()] = None) -> Station:
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        return authenticate(db, token)

    def admin(station: Annotated[Station, Depends(current)]) -> Station:
        if not station.is_admin:
            raise AuthError("Требуются права администратора", 403)
        return station

    Current = Annotated[Station, Depends(current)]
    Admin = Annotated[Station, Depends(admin)]

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(StorageError)
    async def _storage_error(_: Request, exc: StorageError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    # ---------------------------------------------------------------- регистрация

    @app.post("/api/stations/register")
    def register(payload: RegisterIn, request: Request) -> dict:
        return register_station(
            db,
            cfg,
            code=payload.enrollment_code,
            display_name=payload.display_name,
            machine_name=payload.machine_name,
            client_version=payload.client_version,
            ip=_client_ip(request),
        )

    @app.post("/api/auth/token")
    def token(payload: TokenIn, request: Request) -> dict:
        # Обновили сервер — семь .exe старой версии в сети. Понятный отказ лучше
        # непонятных ошибок в середине передачи.
        if version_tuple(payload.client_version) < version_tuple(cfg.server.min_client_version):
            raise AuthError(
                f"Версия клиента {payload.client_version} устарела, "
                f"требуется {cfg.server.min_client_version}. Обновите программу.",
                426,
            )
        return issue_token(
            db,
            cfg,
            station_id=payload.station_id,
            secret=payload.secret,
            client_version=payload.client_version,
            ip=_client_ip(request),
        )

    @app.post("/api/auth/logout")
    def logout(authorization: Annotated[str | None, Header()] = None) -> dict:
        if authorization and authorization.lower().startswith("bearer "):
            db.execute("DELETE FROM tokens WHERE token = ?", (authorization[7:].strip(),))
        return {"ok": True}

    # ---------------------------------------------------------------- справочники

    @app.get("/api/directory")
    def directory(station: Current) -> list[dict]:
        """Список станций — он же адресная книга (2.10)."""
        rows = db.query(
            "SELECT id, display_name, last_seen_at, client_version FROM stations"
            " WHERE is_active = 1 ORDER BY display_name"
        )
        limit = cfg.server.presence_timeout_sec
        return [
            {
                "station_id": r["id"],
                "display_name": r["display_name"],
                "online": (age := age_seconds(r["last_seen_at"])) is not None and age < limit,
                "last_seen_at": r["last_seen_at"],
                "client_version": r["client_version"],
                "self": r["id"] == station.id,
            }
            for r in rows
        ]

    @app.get("/api/me")
    def me(station: Current) -> dict:
        row = db.one("SELECT * FROM stations WHERE id = ?", (station.id,))
        return {
            "station_id": row["id"],
            "display_name": row["display_name"],
            "machine_name": row["machine_name"],
            "is_admin": bool(row["is_admin"]),
            "client_version": row["client_version"],
        }

    @app.patch("/api/me")
    def rename_me(payload: RenameIn, station: Current) -> dict:
        return _rename(station.id, payload.display_name, actor=station.id)

    # ---------------------------------------------------------------- отправка

    @app.post("/api/messages")
    def create_message(payload: MessageIn, station: Current) -> dict:
        message_id = msg.create_message(
            db, cfg, station.id, payload.subject, payload.body, payload.recipients
        )
        return {"message_id": message_id}

    @app.post("/api/messages/{message_id}/attachments/init")
    def init_attachment(message_id: int, payload: AttachmentIn, station: Current) -> dict:
        return msg.init_attachment(
            db, cfg, station.id, message_id, payload.name, payload.size, payload.sha256
        )

    @app.put("/api/uploads/{upload_id}/chunk/{index}")
    async def put_chunk(upload_id: str, index: int, request: Request, station: Current) -> dict:
        session = db.one(
            "SELECT station_id FROM upload_sessions WHERE id = ?", (upload_id,)
        )
        if session is None:
            raise StorageError("Сессия загрузки не найдена", 404)
        if session["station_id"] != station.id:
            raise StorageError("Чужая сессия загрузки", 403)

        # Тело читаем потоком: файл целиком в память не поднимается никогда (2.7).
        chunks: list[bytes] = []
        async for block in request.stream():
            chunks.append(block)
        written = await asyncio.to_thread(
            storage.write_chunk, db, cfg, upload_id, index, iter(chunks)
        )
        return {"received": index, "bytes": written}

    @app.get("/api/uploads/{upload_id}/status")
    def upload_status(upload_id: str, station: Current) -> dict:
        return storage.session_status(db, upload_id)

    @app.post("/api/uploads/{upload_id}/commit", status_code=202)
    async def commit(upload_id: str, station: Current) -> dict:
        """202 Accepted: сборка идёт фоном, готовность видна по state вложения (2.7).

        Для файла в 5 ГБ это чтение 5 ГБ с диска — держать на этом HTTP-запрос нельзя.
        """
        session = db.one("SELECT * FROM upload_sessions WHERE id = ?", (upload_id,))
        if session is None:
            raise StorageError("Сессия загрузки не найдена", 404)
        if session["station_id"] != station.id:
            raise StorageError("Чужая сессия загрузки", 403)

        attachment_id = session["attachment_id"]
        state = db.scalar("SELECT state FROM attachments WHERE id = ?", (attachment_id,))
        if state == "ready":
            return {"attachment_id": attachment_id, "state": "ready", "already": True}

        # Состояние пишется в БД, а не только возвращается в ответе: иначе клиент,
        # опрашивающий /attachments/{id}, увидит 'uploading' и решит, что сборка провалилась.
        db.execute(
            "UPDATE attachments SET state = 'assembling' WHERE id = ?", (attachment_id,)
        )
        asyncio.create_task(asyncio.to_thread(_safe_assemble, upload_id))
        return {"attachment_id": attachment_id, "state": "assembling"}

    def _safe_assemble(upload_id: str) -> None:
        try:
            storage.assemble(db, cfg, upload_id)
        except Exception as exc:  # noqa: BLE001 — фоновая задача не должна ронять сервер
            log.warning("сборка %s не удалась: %s", upload_id, exc)

    @app.get("/api/attachments/{attachment_id}")
    def attachment_state(attachment_id: int, station: Current) -> dict:
        row = db.one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
        if row is None:
            raise StorageError("Вложение не найдено", 404)
        return {
            "attachment_id": row["id"],
            "state": row["state"],
            "size": row["size"],
            "original_name": row["original_name"],
        }

    @app.post("/api/messages/{message_id}/send")
    def send(message_id: int, station: Current) -> dict:
        return msg.send_message(db, station.id, message_id)

    @app.post("/api/messages/{message_id}/revoke")
    def revoke(message_id: int, station: Current) -> dict:
        """Отозвать письмо, пока его никто не забрал."""
        return msg.revoke_message(db, station.id, message_id)

    # ---------------------------------------------------------------- получение

    @app.get("/api/inbox")
    def get_inbox(station: Current) -> list[dict]:
        return msg.inbox(db, station.id)

    @app.get("/api/sent")
    def get_sent(station: Current) -> list[dict]:
        return msg.sent(db, station.id)

    @app.get("/api/messages/{message_id}")
    def get_message(message_id: int, station: Current) -> dict:
        return msg.get_message(db, station.id, message_id)

    @app.post("/api/messages/{message_id}/read")
    def read(message_id: int, station: Current) -> dict:
        msg.mark_read(db, station.id, message_id)
        return {"ok": True}

    @app.post("/api/messages/{message_id}/ack")
    def ack(message_id: int, station: Current) -> dict:
        msg.mark_downloaded(db, station.id, message_id)
        return {"ok": True}

    @app.delete("/api/messages/{message_id}")
    def hide(message_id: int, station: Current) -> dict:
        msg.hide_message(db, station.id, message_id)
        return {"ok": True}

    @app.get("/api/attachments/{attachment_id}/download")
    def download(
        attachment_id: int,
        station: Current,
        range: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        row = msg.attachment_for_download(db, station.id, attachment_id)
        path = storage.resolve(cfg, row["storage_path"])
        if not path.exists():
            db.execute(
                "UPDATE attachments SET state = 'missing' WHERE id = ?", (attachment_id,)
            )
            raise StorageError("Файл отсутствует на сервере", 410)

        limit = cfg.limits.max_parallel_downloads_per_user
        if not slots.acquire(station.id, limit):
            raise StorageError(
                f"Одновременно можно скачивать не больше {limit} файлов, "
                f"дождитесь окончания текущих",
                429,
            )

        size = path.stat().st_size
        try:
            span = storage.parse_range(range, size)
        except StorageError:
            slots.release(station.id)
            raise

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'attachment; filename="{attachment_id}.bin"',
            "X-Original-Name": row["original_name"].encode("utf-8").hex(),
            "X-SHA256": row["sha256"],
        }
        start, end = span if span else (0, size - 1)

        def stream():
            """Слот освобождается и при обрыве соединения: finally отработает
            и когда клиент отвалился на середине пятигигабайтного файла."""
            try:
                yield from storage.iter_range(path, start, end)
            finally:
                slots.release(station.id)

        if span is None:
            headers["Content-Length"] = str(size)
            return StreamingResponse(
                stream(), media_type="application/octet-stream", headers=headers
            )
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)
        return StreamingResponse(
            stream(),
            status_code=206,
            media_type="application/octet-stream",
            headers=headers,
        )

    # ---------------------------------------------------------------- синхронизация

    @app.get("/api/events")
    def events(station: Current, since: int = 0) -> dict:
        if journal.resync_required(db, since):
            return {"resync_required": True}
        items = journal.events_since(db, station.id, since)
        last = items[-1]["id"] if items else since
        return {"events": items, "last_event_id": last}

    @app.get("/api/health")
    def health() -> dict:
        free = free_space(cfg.storage_path)
        reserved = storage.reserved_bytes(db)
        return {
            "status": "ok",
            "version": app.version,
            "time": utcnow(),
            "free_space": free,
            "free_space_human": human_size(free),
            "reserved": reserved,
            "below_threshold": free - reserved < cfg.storage.min_free_space,
        }

    # ---------------------------------------------------------------- админ

    @app.get("/api/admin/stations")
    def admin_stations(station: Admin) -> list[dict]:
        rows = db.query("SELECT * FROM stations ORDER BY display_name")
        limit = cfg.server.presence_timeout_sec
        return [
            {
                "station_id": r["id"],
                "display_name": r["display_name"],
                "machine_name": r["machine_name"],
                "last_ip": r["last_ip"],
                "client_version": r["client_version"],
                "is_active": bool(r["is_active"]),
                "is_admin": bool(r["is_admin"]),
                "last_seen_at": r["last_seen_at"],
                "online": (age := age_seconds(r["last_seen_at"])) is not None and age < limit,
            }
            for r in rows
        ]

    @app.post("/api/admin/enrollment")
    def admin_enrollment(station: Admin, is_admin: bool = False) -> dict:
        result = create_enrollment_code(db, cfg, is_admin=is_admin)
        journal.audit(db, station.id, "enrollment.create", None, is_admin=is_admin)
        return result

    @app.patch("/api/admin/stations/{station_id}")
    def admin_patch_station(station_id: int, payload: StationPatchIn, station: Admin) -> dict:
        row = db.one("SELECT * FROM stations WHERE id = ?", (station_id,))
        if row is None:
            raise StorageError("Станция не найдена", 404)
        if payload.display_name:
            _rename(station_id, payload.display_name, actor=station.id)
        result: dict = {"ok": True}
        if payload.is_active is not None:
            db.execute(
                "UPDATE stations SET is_active = ? WHERE id = ?",
                (int(payload.is_active), station_id),
            )
            if not payload.is_active:
                db.execute("DELETE FROM tokens WHERE station_id = ?", (station_id,))
                # Письма, адресованные станции, никуда не деваются, но забрать их
                # теперь некому. Администратор должен узнать об этом сразу, а не
                # по звонку отправителя через неделю.
                pending = db.query(
                    "SELECT m.id, s.display_name AS sender FROM message_recipients r"
                    " JOIN messages m ON m.id = r.message_id"
                    " JOIN stations s ON s.id = m.sender_id"
                    " WHERE r.recipient_id = ? AND m.status = 'sent'"
                    "   AND r.downloaded_at IS NULL AND r.deleted_by_recipient = 0",
                    (station_id,),
                )
                result["undelivered"] = [
                    {"message_id": r["id"], "sender": r["sender"]} for r in pending
                ]
                if pending:
                    result["warning"] = (
                        f"У станции осталось неполученных писем: {len(pending)}. "
                        f"Файлы на сервере сохранены, но забрать их теперь некому."
                    )
        if payload.is_admin is not None:
            db.execute(
                "UPDATE stations SET is_admin = ? WHERE id = ?",
                (int(payload.is_admin), station_id),
            )
        journal.audit(db, station.id, "station.patch", station_id, **payload.model_dump())
        return result

    @app.post("/api/admin/stations/{station_id}/reset")
    def admin_reset(station_id: int, station: Admin) -> dict:
        return reset_station(db, cfg, station_id)

    @app.get("/api/admin/storage")
    def admin_storage(station: Admin) -> dict:
        free = free_space(cfg.storage_path)
        used = db.scalar("SELECT COALESCE(SUM(size),0) FROM attachments WHERE state='ready'")
        orphaned = msg.find_orphaned(db)
        return {
            "free_space": free,
            "free_space_human": human_size(free),
            "reserved": storage.reserved_bytes(db),
            "used_by_attachments": used,
            "min_free_space": cfg.storage.min_free_space,
            "below_threshold": free < cfg.storage.min_free_space,
            "orphaned": orphaned,
            "missing": [
                r["id"] for r in db.query("SELECT id FROM attachments WHERE state='missing'")
            ],
        }

    @app.post("/api/admin/storage/verify")
    def admin_verify(station: Admin) -> dict:
        return recovery.verify_storage(db, cfg).as_dict()

    @app.post("/api/admin/housekeeping")
    def admin_sweep(station: Admin, backup: bool = False) -> dict:
        """Ручной запуск уборки — то же, что делает планировщик по расписанию."""
        report = hk.sweep(db, cfg, force_backup=backup)
        journal.audit(db, station.id, "housekeeping.manual", None, **report.as_dict())
        return report.as_dict()

    @app.delete("/api/admin/attachments/{attachment_id}")
    def admin_delete_attachment(attachment_id: int, station: Admin) -> dict:
        """Удаляется содержимое, строка остаётся: история переписки и аудит целы (2.3)."""
        row = db.one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
        if row is None:
            raise StorageError("Вложение не найдено", 404)
        if row["storage_path"]:
            storage.resolve(cfg, row["storage_path"]).unlink(missing_ok=True)
        db.execute(
            "UPDATE attachments SET state = 'deleted', storage_path = NULL WHERE id = ?",
            (attachment_id,),
        )
        journal.audit(db, station.id, "attachment.delete", attachment_id, size=row["size"])
        return {"ok": True}

    @app.get("/api/admin/audit")
    def admin_audit(station: Admin, limit: int = 200) -> list[dict]:
        rows = db.query(
            "SELECT a.*, s.display_name AS station FROM audit_log a"
            " LEFT JOIN stations s ON s.id = a.station_id"
            " ORDER BY a.id DESC LIMIT ?",
            (min(limit, 1000),),
        )
        return [dict(r) for r in rows]

    def _rename(station_id: int, display_name: str, *, actor: int) -> dict:
        display_name = display_name.strip()
        clash = db.one(
            "SELECT id FROM stations WHERE display_name = ? AND id != ?",
            (display_name, station_id),
        )
        if clash:
            # Две «Бухгалтерии» в списке — это файл, отправленный не туда (2.3).
            raise StorageError(f"Станция с именем «{display_name}» уже существует", 409)
        db.execute(
            "UPDATE stations SET display_name = ? WHERE id = ?", (display_name, station_id)
        )
        journal.audit(db, actor, "station.rename", station_id, display_name=display_name)
        for row in db.query("SELECT id FROM stations WHERE is_active = 1 AND id != ?", (station_id,)):
            journal.emit(db, row["id"], journal.RENAMED, station_id)
        return {"ok": True, "display_name": display_name}

    return app


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def build(config_path: str | Path, *, background: bool = True) -> FastAPI:
    from .config import load_config

    cfg = load_config(config_path)
    cfg.ensure_dirs()
    _setup_logging(cfg)
    db = Database(cfg.db_path)
    db.init_schema()
    # Восстановление прогоняется до того, как служба начнёт принимать запросы (2.12).
    recovery.run(db, cfg)
    return create_app(cfg, db, background=background)


def _setup_logging(cfg: Config) -> None:
    cfg.logs_path.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        cfg.logs_path / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("filepost")
    if not root.handlers:
        root.addHandler(handler)
        root.setLevel(logging.INFO)
