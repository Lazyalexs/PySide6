"""TransferManager: очередь передач в отдельных потоках. Раздел 3.4.

Не более 1–2 потоков одновременно: иначе они просто делят один и тот же канал.
Состояние очереди — в client.db, поэтому перезапуск приложения передачу продолжает,
а не начинает заново.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .api import ApiClient, ApiError, ClientTooOld, NoSpaceOnServer, Offline
from .store import LocalStore
from .util import utcnow

log = logging.getLogger("filepost.transfers")

READ_BLOCK = 1024 * 1024
RETRY_DELAYS = [1, 2, 5, 10, 30]
MB = 1024 * 1024


class RateLimiter:
    """Ограничение скорости отдачи. Настройка из 3.7.

    Выглядит лишним в гигабитной сети, но именно оно спасает, когда кто-то
    отправляет 20 ГБ в разгар рабочего дня и остальные начинают жаловаться
    на тормоза.

    Считаем по факту переданного: после каждого чанка смотрим, сколько времени
    он должен был занять при заданном пределе, и досыпаем разницу. Так средняя
    скорость выходит на предел без дробления чанков.
    """

    def __init__(self, limit_mbps: int = 0) -> None:
        self.limit = max(0, limit_mbps) * MB
        self._started = time.monotonic()
        self._sent = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def reset(self) -> None:
        self._started = time.monotonic()
        self._sent = 0

    def account(self, size: int, should_stop: Callable[[], bool] | None = None) -> float:
        """Учесть переданные байты и подождать, если обгоняем предел."""
        if not self.enabled:
            return 0.0
        self._sent += size
        expected = self._sent / self.limit
        elapsed = time.monotonic() - self._started
        delay = expected - elapsed
        if delay <= 0:
            return 0.0
        # Спим короткими шагами, чтобы пауза и отмена срабатывали сразу,
        # а не через минуту ожидания на медленном пределе.
        remaining = delay
        while remaining > 0:
            if should_stop and should_stop():
                break
            step = min(0.2, remaining)
            time.sleep(step)
            remaining -= step
        return delay


@dataclass
class Progress:
    transfer_id: int
    state: str
    transferred: int
    size: int
    speed: float = 0.0
    eta: float | None = None
    error: str | None = None

    @property
    def percent(self) -> float:
        return 100.0 * self.transferred / self.size if self.size else 0.0


def sha256_file(path: Path, on_block: Callable[[int], None] | None = None) -> str:
    """SHA-256 целого файла считается на лету при чтении и уходит в init (3.4)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(READ_BLOCK):
            digest.update(block)
            if on_block:
                on_block(len(block))
    return digest.hexdigest()


class TransferManager:
    def __init__(
        self,
        api: ApiClient,
        store: LocalStore,
        *,
        parallel: int = 2,
        downloads_dir: Path | None = None,
        partial_dir: Path | None = None,
        on_progress: Callable[[Progress], None] | None = None,
        on_name_clash: str = "rename",
        upload_limit_mbps: int = 0,
        download_limit_mbps: int = 0,
        on_speed_sample: Callable[[float], None] | None = None,
    ) -> None:
        self.api = api
        self.store = store
        self.parallel = max(1, parallel)
        self.downloads_dir = Path(downloads_dir or ".")
        self.partial_dir = Path(partial_dir or ".")
        self.on_progress = on_progress
        self.on_name_clash = on_name_clash
        # Предел общий на все потоки: ограничивать нужно канал, а не каждую передачу.
        self.limiter = RateLimiter(upload_limit_mbps)
        self.download_limiter = RateLimiter(download_limit_mbps)
        #: сюда уходит фактическая скорость завершённых передач — по ней
        #: считается оценка «примерно N минут» в окне отправки (3.2)
        self.on_speed_sample = on_speed_sample

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._workers: list[threading.Thread] = []
        self._active: set[int] = set()
        self._paused: set[int] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ жизненный цикл

    def start(self) -> None:
        if self._workers:
            return
        self._stop.clear()
        for i in range(self.parallel):
            thread = threading.Thread(target=self._loop, name=f"transfer-{i}", daemon=True)
            thread.start()
            self._workers.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._workers:
            thread.join(timeout=15)
        self._workers.clear()

    def resume_pending(self) -> int:
        """После запуска: активные на момент падения задания возвращаются в очередь."""
        pending = self.store.pending_transfers()
        for item in pending:
            if item["state"] in ("active", "verifying"):
                self.store.update_transfer(item["id"], state="queued")
        self._wake.set()
        return len(pending)

    # ------------------------------------------------------------------ постановка

    def enqueue_upload(self, path: Path, message_id: int, peer: str) -> int:
        path = Path(path)
        return self.store.add_transfer(
            direction="upload",
            state="queued",
            file_path=str(path),
            file_name=path.name,
            size=path.stat().st_size,
            transferred=0,
            message_id=message_id,
            peer=peer,
            created_at=utcnow(),
        )

    def enqueue_download(self, attachment: dict, message_id: int, peer: str) -> int:
        transfer_id = self.store.add_transfer(
            direction="download",
            state="queued",
            file_name=attachment["original_name"],
            size=attachment["size"],
            transferred=0,
            sha256=attachment.get("sha256"),
            message_id=message_id,
            attachment_id=attachment["id"],
            peer=peer,
            created_at=utcnow(),
        )
        self._wake.set()
        return transfer_id

    def pause(self, transfer_id: int) -> None:
        with self._lock:
            self._paused.add(transfer_id)
        self.store.update_transfer(transfer_id, state="paused")

    def resume(self, transfer_id: int) -> None:
        with self._lock:
            self._paused.discard(transfer_id)
        self.store.update_transfer(transfer_id, state="queued", error=None)
        self._wake.set()

    def cancel(self, transfer_id: int) -> None:
        with self._lock:
            self._paused.add(transfer_id)
        item = self.store.transfer(transfer_id)
        if item and item["direction"] == "download":
            self._partial_path(item).unlink(missing_ok=True)
        self.store.remove_transfer(transfer_id)

    def kick(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------ рабочий цикл

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._claim()
            if item is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                if item["direction"] == "upload":
                    self._do_upload(item)
                else:
                    self._do_download(item)
            except Exception:  # noqa: BLE001 — поток очереди не должен умирать
                log.exception("передача %s упала", item["id"])
                self._fail(item, "Внутренняя ошибка, подробности в журнале")
            finally:
                with self._lock:
                    self._active.discard(item["id"])

    def _claim(self) -> dict | None:
        with self._lock:
            for item in self.store.transfers(["queued"]):
                if item["id"] in self._active or item["id"] in self._paused:
                    continue
                self._active.add(item["id"])
                self.store.update_transfer(item["id"], state="active")
                return item
        return None

    def _is_paused(self, transfer_id: int) -> bool:
        with self._lock:
            return transfer_id in self._paused or self._stop.is_set()

    # ------------------------------------------------------------------ отправка

    def _do_upload(self, item: dict) -> None:
        path = Path(item["file_path"])
        if not path.exists():
            self._fail(item, f"Файл не найден: {path}")
            return

        sha = item.get("sha256") or sha256_file(path)
        self.store.update_transfer(item["id"], sha256=sha)

        upload_id = item.get("upload_id")
        attachment_id = item.get("attachment_id")
        if not upload_id:
            try:
                init = self.api.init_attachment(
                    item["message_id"], item["file_name"], item["size"], sha
                )
            except NoSpaceOnServer as exc:
                # Не ошибка сети: повторять с нарастающей паузой бессмысленно (3.4).
                self._fail(item, f"{exc.message}. Обратитесь к администратору")
                return
            except (ClientTooOld, ApiError) as exc:
                self._fail(item, exc.message)
                return
            except Offline:
                self._requeue(item)
                return
            upload_id = init["upload_id"]
            attachment_id = init["attachment_id"]
            self.store.update_transfer(
                item["id"], upload_id=upload_id, attachment_id=attachment_id
            )

        try:
            status = self.api.upload_status(upload_id)
        except Offline:
            self._requeue(item)
            return
        chunk_size = status["chunk_size"]
        received = set(status["received_chunks"])
        total_chunks = status["total_chunks"]

        transferred = min(len(received) * chunk_size, item["size"])
        started = time.monotonic()

        with path.open("rb") as fh:
            for index in range(total_chunks):
                if self._is_paused(item["id"]):
                    self.store.update_transfer(item["id"], transferred=transferred)
                    return
                if index in received:
                    continue
                fh.seek(index * chunk_size)
                block = fh.read(chunk_size)
                if not self._send_chunk(item, upload_id, index, block):
                    return
                transferred = min(transferred + len(block), item["size"])
                self.store.update_transfer(item["id"], transferred=transferred)
                self._emit(item, "active", transferred, started)
                self.limiter.account(len(block), lambda: self._is_paused(item["id"]))

        # Фаза «Проверка»: сборка и сверка sha256 идут на сервере (2.7).
        self.store.update_transfer(item["id"], state="verifying")
        self._emit(item, "verifying", item["size"], started)
        try:
            self.api.commit(upload_id)
        except Offline:
            self._requeue(item)
            return
        except ApiError as exc:
            self._fail(item, exc.message)
            return

        state = self._await_ready(attachment_id)
        if state == "ready":
            self.store.update_transfer(item["id"], state="done", transferred=item["size"])
            self._emit(item, "done", item["size"], started)
        elif state == "offline":
            self._requeue(item)
        else:
            self._fail(item, "Ошибка проверки контрольной суммы, повторите отправку")

    def _send_chunk(self, item: dict, upload_id: str, index: int, block: bytes) -> bool:
        """Повторы с нарастающей паузой: обрыв связи — нормальное состояние (3.4)."""
        for attempt, delay in enumerate([0, *RETRY_DELAYS]):
            if delay:
                time.sleep(delay)
            if self._is_paused(item["id"]):
                return False
            try:
                self.api.put_chunk(upload_id, index, block)
                return True
            except Offline:
                continue
            except ApiError as exc:
                self._fail(item, exc.message)
                return False
        self._requeue(item)
        return False

    def _await_ready(self, attachment_id: int, tries: int = 1200) -> str:
        """Ждём окончания фазы «Проверка» на сервере.

        Переходными считаются оба состояния: `assembling` выставляется при commit,
        но между PUT последнего чанка и записью этого состояния вложение ещё
        `uploading` — не приняв это за отказ, иначе гонка даёт ложную ошибку.
        """
        for _ in range(tries):
            try:
                state = self.api.attachment(attachment_id)["state"]
            except Offline:
                return "offline"
            except ApiError:
                return "failed"
            if state not in ("assembling", "uploading"):
                return state
            time.sleep(0.5)
        return "failed"

    # ------------------------------------------------------------------ приём

    def _do_download(self, item: dict) -> None:
        partial = self._partial_path(item)
        partial.parent.mkdir(parents=True, exist_ok=True)
        start = partial.stat().st_size if partial.exists() else 0
        if start > item["size"]:  # файл на сервере подменили или .part битый
            partial.unlink()
            start = 0

        started = time.monotonic()
        transferred = start
        try:
            with partial.open("ab") as fh:
                for block in self.api.download_range(item["attachment_id"], start):
                    if self._is_paused(item["id"]):
                        self.store.update_transfer(item["id"], transferred=transferred)
                        return
                    fh.write(block)
                    transferred += len(block)
                    self.store.update_transfer(item["id"], transferred=transferred)
                    self._emit(item, "active", transferred, started)
                    self.download_limiter.account(
                        len(block), lambda: self._is_paused(item["id"])
                    )
        except Offline:
            self._requeue(item)
            return
        except ApiError as exc:
            self._fail(item, exc.message)
            return

        self.store.update_transfer(item["id"], state="verifying")
        self._emit(item, "verifying", transferred, started)
        if item.get("sha256") and sha256_file(partial) != item["sha256"]:
            partial.unlink(missing_ok=True)
            self._fail(item, "Контрольная сумма не совпала, файл скачан заново")
            return

        target = self._unique_target(item["file_name"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(partial), str(target))
        self.store.update_transfer(
            item["id"], state="done", transferred=item["size"], file_path=str(target)
        )
        self._emit(item, "done", item["size"], started)

        try:
            self.api.ack(item["message_id"])
        except (Offline, ApiError):
            log.info("ack по сообщению %s уйдёт позже", item["message_id"])

    def _partial_path(self, item: dict) -> Path:
        return self.partial_dir / f"{item['attachment_id']}_{item['file_name']}.part"

    def _unique_target(self, name: str) -> Path:
        target = self.downloads_dir / name
        if self.on_name_clash == "replace" or not target.exists():
            return target
        stem, suffix = target.stem, target.suffix
        for n in range(1, 1000):
            candidate = self.downloads_dir / f"{stem} ({n}){suffix}"
            if not candidate.exists():
                return candidate
        return target

    # ------------------------------------------------------------------ состояния

    def _requeue(self, item: dict) -> None:
        """Нет связи — задание ждёт в очереди и уйдёт само, когда связь вернётся (3.6)."""
        self.store.update_transfer(item["id"], state="queued")

    def _fail(self, item: dict, message: str) -> None:
        self.store.update_transfer(item["id"], state="error", error=message)
        self._emit(item, "error", item.get("transferred", 0), None, error=message)

    def _emit(
        self,
        item: dict,
        state: str,
        transferred: int,
        started: float | None,
        error: str | None = None,
    ) -> None:
        speed = 0.0
        eta = None
        if started:
            elapsed = max(time.monotonic() - started, 0.001)
            speed = transferred / elapsed
            if speed > 0 and item["size"] > transferred:
                eta = (item["size"] - transferred) / speed

        # Замер копим только по завершившимся передачам заметного размера:
        # на мелких файлах скорость меряет накладные расходы, а не канал.
        if (
            state == "done"
            and speed > 0
            and self.on_speed_sample
            and item["size"] >= MB
        ):
            self.on_speed_sample(speed)

        if self.on_progress:
            self.on_progress(
                Progress(item["id"], state, transferred, item["size"], speed, eta, error)
            )
