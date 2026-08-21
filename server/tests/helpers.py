from __future__ import annotations

import time

#: Переходные состояния сборки: `uploading` — чанки ещё принимаются либо commit
#: только что принят, `assembling` — сборка идёт фоном (2.7).
IN_PROGRESS = ("uploading", "assembling")


def wait_ready(station, attachment_id: int, timeout: float = 30.0) -> str:
    """commit отвечает 202 и собирает фоном — готовность видна по state вложения (2.7)."""
    deadline = time.monotonic() + timeout
    state = "unknown"
    while time.monotonic() < deadline:
        state = station.get(f"/api/attachments/{attachment_id}").json()["state"]
        if state not in IN_PROGRESS:
            return state
        time.sleep(0.05)
    return state
