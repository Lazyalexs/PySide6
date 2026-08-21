"""События, курсор синхронизации, присутствие и права. Разделы 2.3, 2.10, 2.11."""

from __future__ import annotations

import os

from filepost.db import Database
from helpers import wait_ready


def test_events_deliver_new_message(buh, sklad):
    r = sklad.get("/api/events?since=0")
    assert r.json()["events"] == []

    data = os.urandom(256)
    message_id, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"

    events = sklad.get("/api/events?since=0").json()
    types = [e["type"] for e in events["events"]]
    assert "new_message" in types
    assert events["last_event_id"] > 0

    # Курсор двигается: повторный опрос с ним ничего нового не приносит.
    cursor = events["last_event_id"]
    assert sklad.get(f"/api/events?since={cursor}").json()["events"] == []


def test_delivery_statuses_reach_sender(buh, sklad):
    data = os.urandom(256)
    message_id, attachment_id = buh.send_file([sklad.station_id], data)
    assert wait_ready(buh, attachment_id) == "ready"
    cursor = buh.get("/api/events?since=0").json()["last_event_id"]

    sklad.post(f"/api/messages/{message_id}/read")
    sklad.post(f"/api/messages/{message_id}/ack")

    events = buh.get(f"/api/events?since={cursor}").json()["events"]
    types = [e["type"] for e in events]
    assert "read" in types and "downloaded" in types


def test_resync_when_cursor_ahead_of_journal(buh, sklad, db: Database):
    """После восстановления БД из копии курсор клиента оказывается впереди журнала.

    Без этой проверки станция замолчала бы навсегда: её since недостижим (2.13).
    """
    buh.send_file([sklad.station_id], os.urandom(128))
    max_id = db.scalar("SELECT MAX(id) FROM events")

    r = sklad.get(f"/api/events?since={max_id + 500}")
    assert r.json() == {"resync_required": True}


def test_resync_when_cursor_older_than_journal(buh, sklad, db: Database):
    """Станция не была в сети дольше срока хранения журнала."""
    for _ in range(3):
        buh.send_file([sklad.station_id], os.urandom(64))
    ids = [r["id"] for r in db.query("SELECT id FROM events ORDER BY id")]
    assert len(ids) >= 3

    # Обрезаем журнал так, как это сделал бы housekeeper: события до последнего удалены.
    db.execute("DELETE FROM events WHERE id < ?", (ids[-1],))

    # Курсор станции остался на первом событии — то, что между ним и остатком, потеряно.
    r = sklad.get(f"/api/events?since={ids[0]}")
    assert r.json() == {"resync_required": True}


def test_no_resync_when_cursor_is_current(buh, sklad, db: Database):
    """Граничный случай: клиент видел всё, журнал обрезан — досинхронизировать нечего."""
    buh.send_file([sklad.station_id], os.urandom(64))
    last = db.scalar("SELECT MAX(id) FROM events")
    r = sklad.get(f"/api/events?since={last}")
    assert r.json()["events"] == []
    assert "resync_required" not in r.json()


def test_events_are_addressed(buh, sklad, make_station):
    """Событие адресовано конкретной станции, чужие его не видят."""
    kadry = make_station("Кадры")
    buh.send_file([sklad.station_id], os.urandom(64))

    assert sklad.get("/api/events?since=0").json()["events"]
    assert kadry.get("/api/events?since=0").json()["events"] == []


def test_rename_propagates(buh, sklad):
    cursor = sklad.get("/api/events?since=0").json()["last_event_id"]
    r = buh.patch("/api/me", json={"display_name": "Бухгалтерия, главный"})
    assert r.status_code == 200

    events = sklad.get(f"/api/events?since={cursor}").json()["events"]
    assert any(e["type"] == "renamed" for e in events)
    names = {s["display_name"] for s in sklad.get("/api/directory").json()}
    assert "Бухгалтерия, главный" in names


def test_rename_to_existing_name_rejected(buh, sklad):
    r = buh.patch("/api/me", json={"display_name": "Склад"})
    assert r.status_code == 409


def test_admin_endpoints_require_admin(buh, sklad):
    assert buh.get("/api/admin/stations").status_code == 200
    assert sklad.get("/api/admin/stations").status_code == 403
    assert sklad.post("/api/admin/enrollment").status_code == 403


def test_admin_can_enroll_new_station(buh, client):
    code = buh.post("/api/admin/enrollment").json()["enrollment_code"]
    r = client.post(
        "/api/stations/register",
        json={"enrollment_code": code, "display_name": "Новая станция"},
    )
    assert r.status_code == 200
    assert r.json()["is_admin"] is False


def test_deactivated_station_cannot_work(buh, sklad):
    buh.patch(f"/api/admin/stations/{sklad.station_id}", json={"is_active": False})
    assert sklad.get("/api/inbox").status_code == 401  # токен отозван вместе с деактивацией
    r = sklad.http.post(
        "/api/auth/token",
        json={"station_id": sklad.station_id, "secret": sklad.secret,
              "client_version": "1.0.0"},
    )
    assert r.status_code == 403


def test_reset_revokes_key(buh, sklad):
    r = buh.post(f"/api/admin/stations/{sklad.station_id}/reset")
    assert "enrollment_code" in r.json()
    # Старый ключ больше не действует.
    r = sklad.http.post(
        "/api/auth/token",
        json={"station_id": sklad.station_id, "secret": sklad.secret,
              "client_version": "1.0.0"},
    )
    assert r.status_code == 401


def test_health_reports_space(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["free_space"] > 0


def test_unauthenticated_requests_rejected(client):
    assert client.get("/api/inbox").status_code == 401
    assert client.get("/api/directory").status_code == 401
    assert client.get("/api/health").status_code == 200  # health открыт намеренно


def test_admin_delete_keeps_history(buh, sklad, db: Database):
    """Удаляется содержимое, строка остаётся — история переписки и аудит целы (2.3)."""
    message_id, attachment_id = buh.send_file([sklad.station_id], os.urandom(256))
    assert wait_ready(buh, attachment_id) == "ready"

    r = buh.delete(f"/api/admin/attachments/{attachment_id}")
    assert r.status_code == 200
    assert db.scalar("SELECT state FROM attachments WHERE id = ?", (attachment_id,)) == "deleted"
    assert sklad.get(f"/api/messages/{message_id}").status_code == 200


def test_audit_records_actions(buh, sklad):
    buh.send_file([sklad.station_id], os.urandom(64))
    entries = buh.get("/api/admin/audit").json()
    actions = {e["action"] for e in entries}
    assert "message.send" in actions
    assert "station.register" in actions
