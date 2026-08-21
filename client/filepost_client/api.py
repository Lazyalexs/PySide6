"""ApiClient: единственный слой, знающий про HTTP. Раздел 3.1.

Выше него Core не должен знать ни про httpx, ни про коды ответов — только про
доменные исключения.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator

import httpx

log = logging.getLogger("filepost.api")


class ApiError(Exception):
    """Ошибка от сервера с человеческим текстом — его можно показывать как есть."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class Offline(ApiError):
    def __init__(self, message: str = "Нет связи с сервером") -> None:
        super().__init__(message, 0)


class NoSpaceOnServer(ApiError):
    """507: повторять с нарастающей паузой бессмысленно, нужен администратор (3.4)."""


class ClientTooOld(ApiError):
    """426: понятный отказ вместо странных ошибок в середине передачи."""


class ApiClient:
    def __init__(self, base_url: str = "", timeout: float = 5.0, version: str = "1.0.0") -> None:
        self.base_url = base_url.rstrip("/")
        self.version = version
        self.token: str = ""
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, read=300.0, write=300.0))

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ низкий уровень

    def _headers(self, extra: dict | None = None) -> dict:
        headers = dict(extra or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, path: str, **kw) -> Any:
        if not self.base_url:
            raise Offline("Адрес сервера не задан")
        url = f"{self.base_url}{path}"
        kw["headers"] = self._headers(kw.pop("headers", None))
        try:
            response = self._http.request(method, url, **kw)
        except httpx.RequestError as exc:
            raise Offline() from exc
        return self._unwrap(response)

    def _unwrap(self, response: httpx.Response) -> Any:
        if response.is_success:
            if not response.content:
                return None
            return response.json()

        message = f"Ошибка сервера ({response.status_code})"
        try:
            body = response.json()
            message = body.get("error") or body.get("detail") or message
            if isinstance(message, list):  # ошибка валидации pydantic
                message = "; ".join(str(m.get("msg", m)) for m in message)
        except Exception:  # noqa: BLE001 — сервер мог ответить не-JSON
            pass

        if response.status_code == 507:
            raise NoSpaceOnServer(message, 507)
        if response.status_code == 426:
            raise ClientTooOld(message, 426)
        raise ApiError(message, response.status_code)

    def get(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> Any:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw) -> Any:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw) -> Any:
        return self.request("DELETE", path, **kw)

    # ------------------------------------------------------------------ вызовы

    def health(self) -> dict:
        return self.get("/api/health")

    def register(self, code: str, display_name: str, machine_name: str) -> dict:
        return self.post(
            "/api/stations/register",
            json={
                "enrollment_code": code,
                "display_name": display_name,
                "machine_name": machine_name,
                "client_version": self.version,
            },
        )

    def authenticate(self, station_id: int, secret: str) -> dict:
        data = self.post(
            "/api/auth/token",
            json={"station_id": station_id, "secret": secret, "client_version": self.version},
        )
        self.token = data["token"]
        return data

    def directory(self) -> list[dict]:
        return self.get("/api/directory")

    def me(self) -> dict:
        return self.get("/api/me")

    def rename(self, display_name: str) -> dict:
        return self.patch("/api/me", json={"display_name": display_name})

    def inbox(self) -> list[dict]:
        return self.get("/api/inbox")

    def sent(self) -> list[dict]:
        return self.get("/api/sent")

    def message(self, message_id: int) -> dict:
        return self.get(f"/api/messages/{message_id}")

    def events(self, since: int) -> dict:
        return self.get("/api/events", params={"since": since})

    def create_message(self, subject: str, body: str, recipients: list[int]) -> int:
        return self.post(
            "/api/messages",
            json={"subject": subject, "body": body, "recipients": recipients},
        )["message_id"]

    def init_attachment(self, message_id: int, name: str, size: int, sha256: str) -> dict:
        return self.post(
            f"/api/messages/{message_id}/attachments/init",
            json={"name": name, "size": size, "sha256": sha256},
        )

    def put_chunk(self, upload_id: str, index: int, data: bytes) -> None:
        self.request("PUT", f"/api/uploads/{upload_id}/chunk/{index}", content=data)

    def upload_status(self, upload_id: str) -> dict:
        return self.get(f"/api/uploads/{upload_id}/status")

    def commit(self, upload_id: str) -> dict:
        return self.post(f"/api/uploads/{upload_id}/commit")

    def attachment(self, attachment_id: int) -> dict:
        return self.get(f"/api/attachments/{attachment_id}")

    def send(self, message_id: int) -> dict:
        return self.post(f"/api/messages/{message_id}/send")

    def revoke(self, message_id: int) -> dict:
        return self.post(f"/api/messages/{message_id}/revoke")

    def mark_read(self, message_id: int) -> None:
        self.post(f"/api/messages/{message_id}/read")

    def ack(self, message_id: int) -> None:
        self.post(f"/api/messages/{message_id}/ack")

    def hide(self, message_id: int) -> None:
        self.delete(f"/api/messages/{message_id}")

    def download_range(self, attachment_id: int, start: int) -> Iterator[bytes]:
        """Скачивание продолжается с нужного байта через заголовок Range (3.4)."""
        if not self.base_url:
            raise Offline("Адрес сервера не задан")
        url = f"{self.base_url}/api/attachments/{attachment_id}/download"
        headers = self._headers({"Range": f"bytes={start}-"} if start else None)
        try:
            with self._http.stream("GET", url, headers=headers) as response:
                if not response.is_success:
                    response.read()
                    self._unwrap(response)
                for block in response.iter_bytes(1024 * 1024):
                    yield block
        except httpx.RequestError as exc:
            raise Offline() from exc

    # ------------------------------------------------------------------ админ

    def admin_stations(self) -> list[dict]:
        return self.get("/api/admin/stations")

    def admin_enrollment(self, is_admin: bool = False) -> dict:
        return self.post("/api/admin/enrollment", params={"is_admin": is_admin})

    def admin_patch_station(self, station_id: int, **fields) -> dict:
        return self.patch(f"/api/admin/stations/{station_id}", json=fields)

    def admin_reset_station(self, station_id: int) -> dict:
        return self.post(f"/api/admin/stations/{station_id}/reset")

    def admin_storage(self) -> dict:
        return self.get("/api/admin/storage")

    def admin_verify(self) -> dict:
        return self.post("/api/admin/storage/verify")

    def admin_housekeeping(self, backup: bool = False) -> dict:
        return self.post("/api/admin/housekeeping", params={"backup": backup})

    def admin_delete_attachment(self, attachment_id: int) -> dict:
        return self.delete(f"/api/admin/attachments/{attachment_id}")

    def admin_audit(self, limit: int = 200) -> list[dict]:
        return self.get("/api/admin/audit", params={"limit": limit})
