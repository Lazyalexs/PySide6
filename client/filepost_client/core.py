"""Core: фасад над ApiClient, LocalStore, TransferManager и SyncAgent.

Про виджеты здесь не знает никто — благодаря этому ядро проверяется автотестами
без запуска окон (3.1). Наружу отдаются только простые словари и колбэки.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from .api import ApiClient, ApiError, ClientTooOld, Offline
from .settings import Settings
from .store import LocalStore
from .transfers import Progress, TransferManager
from .util import machine_name

log = logging.getLogger("filepost.core")

VERSION = "1.0.0"


class Core:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_dirs()
        self.store = LocalStore(settings.db_path)
        self.api = ApiClient(timeout=settings.server.timeout_sec, version=VERSION)
        self.transfers = TransferManager(
            self.api,
            self.store,
            parallel=settings.prefs.parallel_transfers,
            downloads_dir=settings.downloads_dir,
            partial_dir=settings.partial_dir,
            on_progress=self._on_progress,
            on_name_clash=settings.prefs.on_name_clash,
            upload_limit_mbps=settings.prefs.upload_limit_mbps,
        )

        self.online = False
        self.last_error: str = ""
        self.health: dict = {}

        # Колбэки для UI. Ядро не знает, кто на них подписан.
        self.on_state_change: Callable[[], None] | None = None
        self.on_new_message: Callable[[dict], None] | None = None
        self.on_transfer_progress: Callable[[Progress], None] | None = None

        self._sync_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ подключение

    def resolve_server(self) -> str | None:
        """Порядок: url → fallback → discovery. Ни на одном шаге не блокируемся (2.9)."""
        for url in self.settings.candidate_urls():
            probe = ApiClient(url, timeout=self.settings.server.timeout_sec, version=VERSION)
            try:
                probe.health()
                probe.close()
                return url
            except ApiError:
                probe.close()

        if self.settings.server.discovery:
            found = self._discover()
            if found:
                self.settings.remember_url(found)
                return found
        return None

    def _discover(self) -> str | None:
        try:
            from .discovery import discover
        except ImportError:
            return None
        return discover(port=self.settings.server.discovery_port)

    def connect(self) -> bool:
        """Формы входа нет: клиент авторизуется ключом станции сам (2.10)."""
        url = self.resolve_server()
        if not url:
            self.online = False
            self.last_error = "Нет связи с сервером"
            self._notify()
            return False

        self.api.base_url = url.rstrip("/")
        if not self.settings.registered:
            self.online = False
            self.last_error = "Станция не зарегистрирована"
            self._notify()
            return False

        try:
            data = self.api.authenticate(
                self.settings.station.station_id, self.settings.station.secret
            )
        except ClientTooOld as exc:
            self.online = False
            self.last_error = exc.message
            self._notify()
            return False
        except ApiError as exc:
            self.online = False
            self.last_error = exc.message
            self._notify()
            return False

        station = data["station"]
        self.settings.station.display_name = station["display_name"]
        self.settings.station.is_admin = station["is_admin"]
        self.settings.save()
        self.online = True
        self.last_error = ""
        self._notify()
        return True

    def register(self, code: str, display_name: str, url: str = "") -> dict:
        """Однократная регистрация по коду от администратора."""
        if url:
            self.settings.server.url = url
        target = self.resolve_server() or (url or self.settings.server.url)
        if not target:
            raise Offline("Сервер недоступен, проверьте адрес")
        self.api.base_url = target.rstrip("/")

        data = self.api.register(code, display_name or machine_name(), machine_name())
        self.settings.station.station_id = data["station_id"]
        self.settings.station.secret = data["secret"]
        self.settings.station.display_name = data["display_name"]
        self.settings.station.is_admin = data["is_admin"]
        self.settings.server.url = target
        self.settings.save()
        return data

    # ------------------------------------------------------------------ синхронизация

    def start(self) -> None:
        self.transfers.start()
        self.transfers.resume_pending()
        self._stop.clear()
        self._sync_thread = threading.Thread(target=self._sync_loop, name="sync", daemon=True)
        self._sync_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sync_thread:
            self._sync_thread.join(timeout=5)
            self._sync_thread = None
        self.transfers.stop()
        self.api.close()
        self.store.close()

    def _sync_loop(self) -> None:
        interval = max(5, self.settings.server.poll_interval_sec)
        while not self._stop.wait(interval):
            try:
                if not self.online:
                    self.connect()
                if self.online:
                    self.sync()
            except Exception:  # noqa: BLE001 — цикл синхронизации не должен умирать
                log.exception("ошибка синхронизации")

    def sync(self) -> int:
        """Опрос событий. Курсор сохраняется только после их применения (3.5)."""
        try:
            data = self.api.events(self.store.last_event_id)
        except Offline:
            self.online = False
            self.last_error = "Нет связи с сервером"
            self._notify()
            return 0
        except ApiError as exc:
            self.last_error = exc.message
            self._notify()
            return 0

        self.online = True
        self.last_error = ""

        if data.get("resync_required"):
            # Станция не была в сети дольше срока хранения журнала либо БД сервера
            # откатили из копии — перечитываем входящие целиком (2.13).
            log.info("сервер требует полной пересинхронизации")
            self.refresh_all()
            self._refresh_cursor()
            self._notify()
            return 0

        events = data.get("events", [])
        if not events:
            self._refresh_presence()
            return 0

        new_messages: list[dict] = []
        for event in events:
            if event["type"] == "new_message":
                item = self._pull_message(event["object_id"], folder="inbox")
                if item:
                    new_messages.append(item)
            elif event["type"] in ("read", "downloaded"):
                self._pull_message(event["object_id"], folder="sent")
            elif event["type"] == "deleted":
                self.store.remove_message(event["object_id"])
            elif event["type"] == "renamed":
                self._refresh_presence()

        # Порядок важен: сначала применили, потом двинули курсор.
        self.store.set_last_event_id(data["last_event_id"])

        for item in new_messages:
            if self.on_new_message:
                self.on_new_message(item)
            if self.settings.prefs.auto_download:
                self.download_all(item["id"])
        self._notify()
        return len(events)

    def _pull_message(self, message_id: int, folder: str) -> dict | None:
        try:
            item = self.api.message(message_id)
        except ApiError:
            return None
        self.store.upsert_messages(folder, [item])
        return item

    def _refresh_cursor(self) -> None:
        try:
            data = self.api.events(0)
            self.store.set_last_event_id(data.get("last_event_id", 0))
        except ApiError:
            pass

    def refresh_all(self) -> None:
        try:
            self.store.replace_folder("inbox", self.api.inbox())
            self.store.replace_folder("sent", self.api.sent())
            self._refresh_presence()
        except ApiError as exc:
            log.info("обновление не удалось: %s", exc.message)

    def _refresh_presence(self) -> None:
        try:
            self.store.replace_stations(self.api.directory())
        except ApiError:
            pass

    # ------------------------------------------------------------------ чтение

    def inbox(self) -> list[dict]:
        return self.store.messages("inbox")

    def sent(self) -> list[dict]:
        return self.store.messages("sent")

    def message(self, message_id: int) -> dict | None:
        return self.store.message(message_id)

    def stations(self) -> list[dict]:
        return self.store.stations(exclude_self=self.settings.station.station_id)

    def unread_count(self) -> int:
        return self.store.unread_count()

    def mark_read(self, message_id: int) -> None:
        self.store.mark_read_local(message_id)
        try:
            self.api.mark_read(message_id)
        except ApiError:
            pass
        self._notify()

    def hide(self, message_id: int) -> None:
        try:
            self.api.hide(message_id)
        except ApiError as exc:
            self.last_error = exc.message
        self.store.remove_message(message_id)
        self._notify()

    # ------------------------------------------------------------------ отправка

    def compose(self, recipients: list[int], subject: str, body: str,
                files: list[Path], draft_id: int | None = None) -> int:
        """Создаёт сообщение и ставит файлы в очередь. Отправка произойдёт сама.

        Сообщение на сервере появляется только здесь: незаконченное письмо живёт
        в локальных черновиках и серверу неизвестно.
        """
        message_id = self.api.create_message(subject, body, recipients)
        names = {s["station_id"]: s["display_name"] for s in self.stations()}
        peer = ", ".join(names.get(r, str(r)) for r in recipients)
        for path in files:
            self.transfers.enqueue_upload(Path(path), message_id, peer)
        self.transfers.kick()
        if draft_id:
            self.store.remove_draft(draft_id)
        return message_id

    # ------------------------------------------------------------------ черновики

    def save_draft(
        self,
        recipients: list[int],
        subject: str,
        body: str,
        files: list[Path],
        draft_id: int | None = None,
    ) -> int:
        """Сохранить незаконченное письмо. Работает и без связи с сервером."""
        draft_id = self.store.save_draft(
            draft_id=draft_id,
            subject=subject,
            body=body,
            recipients=recipients,
            files=[str(p) for p in files],
        )
        self._notify()
        return draft_id

    def drafts(self) -> list[dict]:
        return self.store.drafts()

    def draft(self, draft_id: int) -> dict | None:
        return self.store.draft(draft_id)

    def delete_draft(self, draft_id: int) -> None:
        self.store.remove_draft(draft_id)
        self._notify()

    def finalize_if_ready(self, message_id: int) -> bool:
        """Все вложения загружены — переводим сообщение в «отправлено»."""
        pending = [
            t
            for t in self.store.transfers(["queued", "active", "verifying"])
            if t["message_id"] == message_id and t["direction"] == "upload"
        ]
        if pending:
            return False
        try:
            self.api.send(message_id)
        except ApiError as exc:
            self.last_error = exc.message
            return False
        self.refresh_all()
        self._notify()
        return True

    def download_all(self, message_id: int) -> list[int]:
        item = self.store.message(message_id) or {}
        peer = item.get("sender") or ""
        ids = []
        for attachment in item.get("attachments", []):
            if attachment.get("state") == "ready":
                ids.append(self.transfers.enqueue_download(attachment, message_id, peer))
        return ids

    # ------------------------------------------------------------------ настройки

    def rename_station(self, display_name: str) -> None:
        self.api.rename(display_name)
        self.settings.station.display_name = display_name
        self.settings.save()
        self._notify()

    def apply_settings(self) -> None:
        """Подхватить изменённые предпочтения без перезапуска клиента.

        Число потоков на лету не меняем: пересоздавать пул посреди активных
        передач дороже, чем дождаться следующего запуска.
        """
        prefs = self.settings.prefs
        self.transfers.downloads_dir = self.settings.downloads_dir
        self.transfers.partial_dir = self.settings.partial_dir
        self.transfers.on_name_clash = prefs.on_name_clash
        self.transfers.limiter.limit = max(0, prefs.upload_limit_mbps) * (1024 * 1024)
        self.transfers.limiter.reset()
        self.settings.ensure_dirs()

    def refresh_health(self) -> dict:
        try:
            self.health = self.api.health()
            self.online = True
        except ApiError:
            self.online = False
            self.health = {}
        return self.health

    # ------------------------------------------------------------------ внутреннее

    def _on_progress(self, progress: Progress) -> None:
        if self.on_transfer_progress:
            self.on_transfer_progress(progress)
        if progress.state == "done":
            item = self.store.transfer(progress.transfer_id)
            if item and item["direction"] == "upload" and item["message_id"]:
                self.finalize_if_ready(item["message_id"])

    def _notify(self) -> None:
        if self.on_state_change:
            self.on_state_change()
