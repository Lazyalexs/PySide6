"""Регистрация станций по одноразовому коду и авторизация ключом. Разделы 2.10, 2.11.

Формы входа у человека нет: клиент получает токен по ключу станции сам, при запуске.
"""

from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import Config
from .db import Database
from .journal import audit
from .util import expired, in_hours, new_enrollment_code, new_secret, new_token, utcnow

_hasher = PasswordHasher()


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Station:
    id: int
    display_name: str
    is_admin: bool
    is_active: bool


def hash_secret(secret: str) -> str:
    return _hasher.hash(secret)


def verify_secret(secret: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, secret)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_enrollment_code(db: Database, cfg: Config, *, is_admin: bool = False) -> dict:
    code = new_enrollment_code()
    expires_at = in_hours(cfg.server.enrollment_ttl_hours)
    db.execute(
        "INSERT INTO enrollment_codes (code, is_admin, expires_at, created_at)"
        " VALUES (?,?,?,?)",
        (code, int(is_admin), expires_at, utcnow()),
    )
    return {"enrollment_code": code, "expires_at": expires_at, "is_admin": is_admin}


def register_station(
    db: Database,
    cfg: Config,
    *,
    code: str,
    display_name: str,
    machine_name: str | None,
    client_version: str | None,
    ip: str | None,
) -> dict:
    """Обменять одноразовый код на постоянный ключ станции."""
    code = (code or "").strip().upper()
    row = db.one("SELECT * FROM enrollment_codes WHERE code = ?", (code,))
    if row is None:
        raise AuthError("Код регистрации не найден", 403)
    if row["used_at"] is not None:
        raise AuthError("Код регистрации уже использован", 403)
    if expired(row["expires_at"]):
        raise AuthError("Срок действия кода регистрации истёк", 403)

    display_name = (display_name or "").strip() or (machine_name or "").strip()
    if not display_name:
        raise AuthError("Не задано имя станции", 400)
    if db.one("SELECT id FROM stations WHERE display_name = ?", (display_name,)):
        raise AuthError(f"Станция с именем «{display_name}» уже существует", 409)

    secret = new_secret()
    station_id = db.insert(
        "INSERT INTO stations (display_name, machine_name, secret_hash, last_ip,"
        " client_version, is_active, is_admin, first_seen_at, last_seen_at)"
        " VALUES (?,?,?,?,?,1,?,?,?)",
        (
            display_name,
            machine_name,
            hash_secret(secret),
            ip,
            client_version,
            int(bool(row["is_admin"])),
            utcnow(),
            utcnow(),
        ),
    )
    db.execute(
        "UPDATE enrollment_codes SET used_at = ?, used_by = ? WHERE code = ?",
        (utcnow(), station_id, code),
    )
    audit(db, station_id, "station.register", station_id, display_name=display_name)
    return {
        "station_id": station_id,
        "secret": secret,
        "display_name": display_name,
        "is_admin": bool(row["is_admin"]),
    }


def issue_token(
    db: Database,
    cfg: Config,
    *,
    station_id: int,
    secret: str,
    client_version: str | None,
    ip: str | None,
) -> dict:
    row = db.one("SELECT * FROM stations WHERE id = ?", (station_id,))
    if row is None or not verify_secret(secret, row["secret_hash"]):
        raise AuthError("Станция не зарегистрирована или ключ недействителен", 401)
    if not row["is_active"]:
        raise AuthError("Станция отключена администратором", 403)

    token = new_token()
    expires_at = in_hours(cfg.server.token_ttl_hours)
    db.execute(
        "INSERT INTO tokens (token, station_id, expires_at) VALUES (?,?,?)",
        (token, station_id, expires_at),
    )
    db.execute(
        "UPDATE stations SET last_seen_at = ?, last_ip = ?, client_version = ? WHERE id = ?",
        (utcnow(), ip, client_version or row["client_version"], station_id),
    )
    # Протухшие токены этой станции не копим.
    db.execute(
        "DELETE FROM tokens WHERE station_id = ? AND expires_at <= ?",
        (station_id, utcnow()),
    )
    return {
        "token": token,
        "expires_at": expires_at,
        "station": {
            "station_id": station_id,
            "display_name": row["display_name"],
            "is_admin": bool(row["is_admin"]),
        },
    }


def authenticate(db: Database, token: str | None) -> Station:
    """Проверка токена + обновление присутствия. Каждый запрос — это же и heartbeat (2.10)."""
    if not token:
        raise AuthError("Требуется авторизация")
    row = db.one(
        "SELECT t.expires_at, s.id, s.display_name, s.is_admin, s.is_active"
        " FROM tokens t JOIN stations s ON s.id = t.station_id WHERE t.token = ?",
        (token,),
    )
    if row is None:
        raise AuthError("Токен недействителен")
    if expired(row["expires_at"]):
        db.execute("DELETE FROM tokens WHERE token = ?", (token,))
        raise AuthError("Срок действия токена истёк")
    if not row["is_active"]:
        raise AuthError("Станция отключена администратором", 403)

    db.execute("UPDATE stations SET last_seen_at = ? WHERE id = ?", (utcnow(), row["id"]))
    return Station(
        id=row["id"],
        display_name=row["display_name"],
        is_admin=bool(row["is_admin"]),
        is_active=bool(row["is_active"]),
    )


def reset_station(db: Database, cfg: Config, station_id: int) -> dict:
    """Отозвать ключ станции и выдать новый код регистрации."""
    row = db.one("SELECT id, is_admin FROM stations WHERE id = ?", (station_id,))
    if row is None:
        raise AuthError("Станция не найдена", 404)
    db.execute("DELETE FROM tokens WHERE station_id = ?", (station_id,))
    db.execute("UPDATE stations SET secret_hash = ? WHERE id = ?", (new_secret(), station_id))
    audit(db, station_id, "station.reset", station_id)
    return create_enrollment_code(db, cfg, is_admin=bool(row["is_admin"]))
