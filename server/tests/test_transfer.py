"""Полный цикл передачи: регистрация, заливка, докачка, скачивание с Range."""

from __future__ import annotations

import hashlib
import os

import pytest

from helpers import wait_ready


def test_registration_and_directory(buh, sklad):
    r = buh.get("/api/directory")
    assert r.status_code == 200
    names = {s["display_name"] for s in r.json()}
    assert names == {"Бухгалтерия, окно 2", "Склад"}
    assert all(s["online"] for s in r.json()), "опрос обновляет присутствие"


def test_enrollment_code_is_single_use(client, cfg, db):
    from filepost.auth import create_enrollment_code

    code = create_enrollment_code(db, cfg)["enrollment_code"]
    first = client.post(
        "/api/stations/register", json={"enrollment_code": code, "display_name": "Первая"}
    )
    assert first.status_code == 200
    second = client.post(
        "/api/stations/register", json={"enrollment_code": code, "display_name": "Вторая"}
    )
    assert second.status_code == 403
    assert "использован" in second.json()["error"]


def test_duplicate_display_name_rejected(client, cfg, db, buh):
    from filepost.auth import create_enrollment_code

    code = create_enrollment_code(db, cfg)["enrollment_code"]
    r = client.post(
        "/api/stations/register",
        json={"enrollment_code": code, "display_name": "Бухгалтерия, окно 2"},
    )
    # Две «Бухгалтерии» в списке — это файл, отправленный не туда (2.3).
    assert r.status_code == 409


def test_full_transfer_cycle(buh, sklad):
    data = os.urandom(3 * 1024 * 1024 + 137)  # три с лишним чанка по 1 МБ
    message_id, attachment_id = buh.send_file([sklad.station_id], data, name="акты.zip")

    assert wait_ready(buh, attachment_id) == "ready"

    inbox = sklad.get("/api/inbox").json()
    assert len(inbox) == 1
    assert inbox[0]["sender"] == "Бухгалтерия, окно 2"
    assert inbox[0]["attachments"][0]["original_name"] == "акты.zip"

    r = sklad.get(f"/api/attachments/{attachment_id}/download")
    assert r.status_code == 200
    assert r.content == data
    assert hashlib.sha256(r.content).hexdigest() == r.headers["x-sha256"]

    sklad.post(f"/api/messages/{message_id}/ack")
    outbox = buh.get("/api/sent").json()
    assert outbox[0]["recipients"][0]["downloaded_at"] is not None


def test_range_download_resumes(buh, sklad):
    data = os.urandom(2 * 1024 * 1024)
    _, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"

    half = len(data) // 2
    r = sklad.get(
        f"/api/attachments/{attachment_id}/download", headers={"Range": f"bytes={half}-"}
    )
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes {half}-{len(data) - 1}/{len(data)}"
    assert r.content == data[half:]

    r = sklad.get(
        f"/api/attachments/{attachment_id}/download", headers={"Range": "bytes=0-1023"}
    )
    assert r.status_code == 206
    assert r.content == data[:1024]

    r = sklad.get(
        f"/api/attachments/{attachment_id}/download",
        headers={"Range": f"bytes={len(data) + 10}-"},
    )
    assert r.status_code == 416


def test_upload_resume_after_break(buh, sklad, cfg):
    """Обрыв связи: часть чанков дошла, клиент спрашивает статус и досылает остальные."""
    data = os.urandom(3 * 1024 * 1024)
    digest = hashlib.sha256(data).hexdigest()

    message_id = buh.post(
        "/api/messages", json={"subject": "t", "body": "", "recipients": [sklad.station_id]}
    ).json()["message_id"]
    upload = buh.post(
        f"/api/messages/{message_id}/attachments/init",
        json={"name": "big.bin", "size": len(data), "sha256": digest},
    ).json()
    chunk_size = upload["chunk_size"]

    # Дошёл только нулевой чанк, дальше связь оборвалась.
    buh.put(f"/api/uploads/{upload['upload_id']}/chunk/0", content=data[:chunk_size])

    status = buh.get(f"/api/uploads/{upload['upload_id']}/status").json()
    assert status["received_chunks"] == [0]
    assert status["total_chunks"] == 3

    missing = set(range(status["total_chunks"])) - set(status["received_chunks"])
    for index in sorted(missing):
        piece = data[index * chunk_size:(index + 1) * chunk_size]
        buh.put(f"/api/uploads/{upload['upload_id']}/chunk/{index}", content=piece)

    buh.post(f"/api/uploads/{upload['upload_id']}/commit")
    assert wait_ready(buh, upload["attachment_id"]) == "ready"


def test_checksum_mismatch_rejected(buh, sklad):
    """Заявленный sha256 не совпал с собранным — отказ, вложение failed."""
    data = os.urandom(1024)
    message_id = buh.post(
        "/api/messages", json={"subject": "t", "body": "", "recipients": [sklad.station_id]}
    ).json()["message_id"]
    upload = buh.post(
        f"/api/messages/{message_id}/attachments/init",
        json={"name": "bad.bin", "size": len(data), "sha256": "0" * 64},
    ).json()
    buh.put(f"/api/uploads/{upload['upload_id']}/chunk/0", content=data)
    buh.post(f"/api/uploads/{upload['upload_id']}/commit")

    assert wait_ready(buh, upload["attachment_id"]) == "failed"
    r = buh.post(f"/api/messages/{message_id}/send")
    assert r.status_code == 409  # с несобранным вложением отправить нельзя


def test_commit_is_idempotent(buh, sklad):
    data = os.urandom(1024)
    _, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"

    session_id = buh.http.app.state.db.scalar(
        "SELECT id FROM upload_sessions WHERE attachment_id = ?", (attachment_id,)
    )
    r = buh.post(f"/api/uploads/{session_id}/commit")
    assert r.status_code == 202
    assert r.json()["already"] is True


def test_send_is_idempotent(buh, sklad):
    data = os.urandom(512)
    message_id, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"
    r = buh.post(f"/api/messages/{message_id}/send")
    assert r.json()["already"] is True
    assert len(sklad.get("/api/inbox").json()) == 1


def test_chunk_reupload_overwrites(buh, sklad):
    """Повторный PUT того же чанка перезаписывает его, а не ломает сборку."""
    data = os.urandom(1024)
    digest = hashlib.sha256(data).hexdigest()
    message_id = buh.post(
        "/api/messages", json={"subject": "t", "body": "", "recipients": [sklad.station_id]}
    ).json()["message_id"]
    upload = buh.post(
        f"/api/messages/{message_id}/attachments/init",
        json={"name": "x.bin", "size": len(data), "sha256": digest},
    ).json()

    buh.put(f"/api/uploads/{upload['upload_id']}/chunk/0", content=os.urandom(1024))
    buh.put(f"/api/uploads/{upload['upload_id']}/chunk/0", content=data)
    buh.post(f"/api/uploads/{upload['upload_id']}/commit")
    assert wait_ready(buh, upload["attachment_id"]) == "ready"


def test_foreign_upload_session_rejected(buh, sklad):
    """Сессия закрыта целиком: и дописать в неё, и просто посмотреть.

    В статусе видно имя, размер и что уже долито — по нему соседний кабинет
    читался бы как на ладони.
    """
    message_id = buh.post(
        "/api/messages", json={"subject": "t", "body": "", "recipients": [sklad.station_id]}
    ).json()["message_id"]
    upload = buh.post(
        f"/api/messages/{message_id}/attachments/init",
        json={"name": "x.bin", "size": 10, "sha256": "a" * 64},
    ).json()
    upload_id = upload["upload_id"]

    assert sklad.put(f"/api/uploads/{upload_id}/chunk/0", content=b"x" * 10).status_code == 403
    assert sklad.get(f"/api/uploads/{upload_id}/status").status_code == 403
    assert sklad.post(f"/api/uploads/{upload_id}/commit").status_code == 403

    # Владельцу — по-прежнему всё.
    assert buh.get(f"/api/uploads/{upload_id}/status").status_code == 200
    # Несуществующая сессия остаётся 404, а не превращается в 403.
    assert buh.get("/api/uploads/00000000-0000-0000-0000-000000000000/status").status_code == 404


def test_download_requires_access(buh, sklad, make_station):
    kadry = make_station("Кадры")
    data = os.urandom(256)
    _, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"
    # Посторонняя станция не должна даже знать, что вложение существует.
    assert kadry.get(f"/api/attachments/{attachment_id}/download").status_code == 404


def test_attachment_state_requires_access(buh, sklad, make_station):
    """Состояние вложения — тоже доступ: в нём лежит имя файла.

    Id последовательные, так что без проверки имена всех файлов в системе
    достаются перебором, хотя скачать их и нельзя.
    """
    kadry = make_station("Кадры")
    _, attachment_id = buh.send_file(
        [sklad.station_id], os.urandom(256), name="зарплата.xlsx"
    )
    assert wait_ready(buh, attachment_id) == "ready"

    assert kadry.get(f"/api/attachments/{attachment_id}").status_code == 404
    # Отправителю и получателю оно по-прежнему видно.
    assert buh.get(f"/api/attachments/{attachment_id}").status_code == 200
    r = sklad.get(f"/api/attachments/{attachment_id}")
    assert r.status_code == 200
    assert r.json()["original_name"] == "зарплата.xlsx"


def test_sender_sees_state_of_unsent_attachment(buh, sklad):
    """Отправитель ходит сюда именно ради неготового: письмо ещё черновик (2.7)."""
    message_id = buh.post(
        "/api/messages", json={"subject": "t", "body": "", "recipients": [sklad.station_id]}
    ).json()["message_id"]
    upload = buh.post(
        f"/api/messages/{message_id}/attachments/init",
        json={"name": "x.bin", "size": 10, "sha256": "a" * 64},
    ).json()
    r = buh.get(f"/api/attachments/{upload['attachment_id']}")
    assert r.status_code == 200
    assert r.json()["state"] == "uploading"
    # А получатель до отправки письма о вложении знать не должен.
    assert sklad.get(f"/api/attachments/{upload['attachment_id']}").status_code == 404


def test_broken_range_header_is_not_a_crash(buh, sklad):
    """Мусор в заголовке — 416, а не 500: заголовки сочиняет не только наш клиент."""
    _, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"
    for header in ("bytes=abc-def", "bytes=-", "bytes=0-xyz", "bytes=--5"):
        r = sklad.get(
            f"/api/attachments/{attachment_id}/download", headers={"Range": header}
        )
        assert r.status_code == 416, f"{header} → {r.status_code}"
    # Неизвестная единица измерения диапазоном не считается — отдаём файл целиком.
    r = sklad.get(
        f"/api/attachments/{attachment_id}/download", headers={"Range": "items=0-1"}
    )
    assert r.status_code == 200


@pytest.mark.parametrize("payload_size", [0, 1, 1024 * 1024])
def test_edge_sizes(buh, sklad, payload_size):
    data = os.urandom(payload_size)
    _, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"
    r = sklad.get(f"/api/attachments/{attachment_id}/download")
    assert r.content == data
