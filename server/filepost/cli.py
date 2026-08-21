"""Консольные утилиты. Раздел 2.11.

Веб-панели нет: повседневное администрирование делается из клиента, а консоль
остаётся для случаев, когда клиент недоступен в принципе — первый запуск,
восстановление доступа, обслуживание БД.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .auth import create_enrollment_code, reset_station
from .config import DEFAULT_PORT, DYNAMIC_PORT_START, default_config_toml, load_config
from .db import Database
from .recovery import verify_storage
from .storage import reserved_bytes
from .util import free_space, human_size, utcnow


def _open(config_path: str) -> tuple:
    cfg = load_config(config_path)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    db.init_schema()
    return cfg, db


def cmd_init(args: argparse.Namespace) -> int:
    """Разрывает задачу про курицу и яйцо: код регистрации выдаёт администратор из
    клиента, но клиента ещё нет ни на одной машине."""
    config_path = Path(args.config)
    if not config_path.exists():
        # Каталог по умолчанию — тот, где лежит сам config.toml: установщик
        # передаёт сюда фактический каталог установки, и пути в конфиге
        # получаются под него, а не под зашитый в код D:\FilePost.
        root = args.root or config_path.parent.resolve()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(default_config_toml(root, args.port), encoding="utf-8")
        print(f"Создан файл настроек: {config_path}")

    cfg, db = _open(args.config)
    # Проверяем не только станции, но и уже выданные коды: иначе повторный init
    # до регистрации первой станции выдал бы второй административный код,
    # и права администратора смогла бы забрать посторонняя машина.
    if db.scalar("SELECT COUNT(*) FROM stations") or db.scalar(
        "SELECT COUNT(*) FROM enrollment_codes"
    ):
        print("База уже инициализирована.", file=sys.stderr)
        print("Новый код для станции:  filepost-server station enroll", file=sys.stderr)
        print(
            "Если потерян доступ администратора:  station enroll --admin",
            file=sys.stderr,
        )
        return 1

    result = create_enrollment_code(db, cfg, is_admin=True)

    # Код дублируется в отдельный файл: его читает установщик, чтобы показать
    # администратору. Разбирать вывод консоли для этого нельзя — там русский
    # текст в кодировке консоли, а код здесь чистый ASCII.
    code_file = cfg.logs_path / "enrollment-code.txt"
    code_file.write_text(result["enrollment_code"], encoding="ascii")

    print(f"База данных создана: {cfg.db_path}")
    print(f"Хранилище: {cfg.storage_path}")
    print()
    print(f"Код регистрации первой станции: {result['enrollment_code']}")
    print("Станция получит права администратора.")
    print(f"Код действует до {result['expires_at']} и только один раз.")
    print(f"Код также сохранён в {code_file}")

    if cfg.server.port >= DYNAMIC_PORT_START:
        print()
        print(f"ВНИМАНИЕ: порт {cfg.server.port} лежит в динамическом диапазоне Windows.")
        print("Зарезервируйте его, иначе служба будет иногда не стартовать после")
        print("перезагрузки — система может отдать порт исходящему соединению:")
        print(
            f"  netsh int ipv4 add excludedportrange protocol=tcp "
            f"startport={cfg.server.port} numberofports=1"
        )
    return 0


def cmd_station_list(args: argparse.Namespace) -> int:
    cfg, db = _open(args.config)
    rows = db.query("SELECT * FROM stations ORDER BY display_name")
    if not rows:
        print("Станций нет.")
        return 0
    print(f"{'Станция':<28} {'Статус':<12} {'Версия':<8} Последняя активность")
    for r in rows:
        status = "отключена" if not r["is_active"] else "активна"
        admin = " *" if r["is_admin"] else ""
        print(
            f"{r['display_name'] + admin:<28} {status:<12} "
            f"{r['client_version'] or '—':<8} {r['last_seen_at'] or '—'}"
        )
    print("\n* — права администратора")
    return 0


def cmd_station_enroll(args: argparse.Namespace) -> int:
    cfg, db = _open(args.config)
    result = create_enrollment_code(db, cfg, is_admin=args.admin)
    print(f"Код регистрации: {result['enrollment_code']}")
    print(f"Действует до {result['expires_at']}, один раз.")
    if args.admin:
        print("Станция получит права администратора.")
    return 0


def cmd_station_reset(args: argparse.Namespace) -> int:
    """Выход из положения, когда административная станция вышла из строя."""
    cfg, db = _open(args.config)
    row = db.one("SELECT id FROM stations WHERE display_name = ?", (args.name,))
    if row is None:
        print(f"Станция «{args.name}» не найдена.", file=sys.stderr)
        return 1
    result = reset_station(db, cfg, row["id"])
    print(f"Ключ станции «{args.name}» отозван.")
    print(f"Новый код регистрации: {result['enrollment_code']}")
    return 0


def cmd_station_admin(args: argparse.Namespace) -> int:
    cfg, db = _open(args.config)
    row = db.one("SELECT id, is_admin FROM stations WHERE display_name = ?", (args.name,))
    if row is None:
        print(f"Станция «{args.name}» не найдена.", file=sys.stderr)
        return 1
    value = 0 if args.revoke else 1
    db.execute("UPDATE stations SET is_admin = ? WHERE id = ?", (value, row["id"]))
    print(f"Права администратора {'сняты' if args.revoke else 'выданы'}: {args.name}")
    return 0


def cmd_storage_report(args: argparse.Namespace) -> int:
    from .messages import find_orphaned

    cfg, db = _open(args.config)
    free = free_space(cfg.storage_path)
    reserved = reserved_bytes(db)
    used = db.scalar("SELECT COALESCE(SUM(size),0) FROM attachments WHERE state='ready'")
    orphaned = find_orphaned(db)
    orphan_size = 0
    if orphaned:
        orphan_size = db.scalar(
            f"SELECT COALESCE(SUM(size),0) FROM attachments WHERE id IN "
            f"({','.join('?' * len(orphaned))})",
            orphaned,
        )
    print(f"Свободно на диске:      {human_size(free)}")
    print(f"Обещано заливкам:       {human_size(reserved)}")
    print(f"Занято вложениями:      {human_size(used)}")
    print(f"Порог блокировки:       {human_size(cfg.storage.min_free_space)}")
    if free < cfg.storage.min_free_space:
        print("  ВНИМАНИЕ: свободное место ниже порога, приём новых файлов заблокирован")
    print()
    print(f"Ничейных вложений:      {len(orphaned)} ({human_size(orphan_size)})")
    missing = db.scalar("SELECT COUNT(*) FROM attachments WHERE state='missing'")
    print(f"Потерянных файлов:      {missing}")
    return 0


def cmd_storage_verify(args: argparse.Namespace) -> int:
    """Обязательный шаг после восстановления БД из копии (2.13)."""
    cfg, db = _open(args.config)
    report = verify_storage(db, cfg)
    print(f"Проверено записей: {report.checked}")
    print(f"Записей без файла на диске: {len(report.missing_files)}")
    for item in report.missing_files[:20]:
        print(f"  вложение {item['attachment_id']}: {item['name']}")
    print(f"Файлов без записи в БД: {len(report.orphan_files)}")
    for path in report.orphan_files[:20]:
        print(f"  {path}")
    if report.missing_files or report.orphan_files:
        print("\nБД и хранилище разошлись. Записи помечены как missing.")
    else:
        print("\nРасхождений нет.")
    return 0


def cmd_housekeeping(args: argparse.Namespace) -> int:
    from .housekeeper import sweep

    cfg, db = _open(args.config)
    report = sweep(db, cfg, force_backup=args.backup)
    print(f"Отпущено простаивающих резервов:  {report.stale_reservations}")
    print(f"Убрано брошенных загрузок:        {report.abandoned_uploads}")
    print(f"Обрезано событий:                 {report.events_trimmed}")
    print(f"Помечено ничейными:               {len(report.orphaned_marked)}")
    print(f"Удалено по сроку:                 {len(report.retention_deleted)}")
    if report.backup_path:
        print(f"Резервная копия:                  {report.backup_path}")
    if report.low_space:
        print("\nВНИМАНИЕ: свободное место ниже порога")
    return 0


def cmd_db_backup(args: argparse.Namespace) -> int:
    """Копировать файл базы в режиме WAL нельзя — только согласованный снимок (2.13)."""
    cfg, db = _open(args.config)
    target = Path(args.path)
    db.backup_to(target)
    size = target.stat().st_size
    print(f"Резервная копия создана: {target} ({human_size(size)})")
    print(f"Время: {utcnow()}")
    return 0


def cmd_db_verify(args: argparse.Namespace) -> int:
    """Копия, которую ни разу не проверяли, резервной копией не является."""
    import sqlite3

    path = Path(args.path)
    if not path.exists():
        print(f"Файл не найден: {path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        stations = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
        msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    print(f"integrity_check: {result}")
    print(f"Станций в копии: {stations}, сообщений: {msgs}")
    return 0 if result == "ok" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="filepost", description="FilePost: сервер и утилиты")
    parser.add_argument("--config", default="config.toml", help="путь к config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="создать БД и код регистрации первой станции")
    init.add_argument(
        "--root",
        default=None,
        help="каталог установки: под него подставляются пути в новом config.toml",
    )
    init.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"порт службы в новом config.toml (по умолчанию {DEFAULT_PORT})",
    )
    init.set_defaults(func=cmd_init)

    serve = sub.add_parser("serve", help="запустить сервер")
    serve.set_defaults(func=cmd_serve)

    station = sub.add_parser("station", help="управление станциями").add_subparsers(
        dest="action", required=True
    )
    station.add_parser("list", help="список станций").set_defaults(func=cmd_station_list)

    enroll = station.add_parser("enroll", help="выдать код регистрации")
    enroll.add_argument("--admin", action="store_true", help="с правами администратора")
    enroll.set_defaults(func=cmd_station_enroll)

    reset = station.add_parser("reset", help="отозвать ключ и выдать новый код")
    reset.add_argument("name", help="отображаемое имя станции")
    reset.set_defaults(func=cmd_station_reset)

    admin = station.add_parser("admin", help="выдать или снять права администратора")
    admin.add_argument("name")
    admin.add_argument("--revoke", action="store_true", help="снять права")
    admin.set_defaults(func=cmd_station_admin)

    storage = sub.add_parser("storage", help="хранилище").add_subparsers(
        dest="action", required=True
    )
    storage.add_parser("report", help="занято, свободно, ничейные").set_defaults(
        func=cmd_storage_report
    )
    storage.add_parser("verify", help="сверка БД и диска").set_defaults(func=cmd_storage_verify)

    sweep = sub.add_parser("housekeeping", help="разовый прогон уборки")
    sweep.add_argument("--backup", action="store_true", help="сделать бэкап немедленно")
    sweep.set_defaults(func=cmd_housekeeping)

    database = sub.add_parser("db", help="обслуживание базы").add_subparsers(
        dest="action", required=True
    )
    backup = database.add_parser("backup", help="резервная копия живой базы")
    backup.add_argument("path")
    backup.set_defaults(func=cmd_db_backup)

    verify = database.add_parser("verify", help="проверить резервную копию")
    verify.add_argument("path")
    verify.set_defaults(func=cmd_db_verify)

    return parser


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import build

    cfg = load_config(args.config)
    app = build(args.config)
    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
        # Умолчания Uvicorn рассчитаны на API с JSON, а здесь ходят гигабайты.
        # Keep-alive длиннее: между чанками бывают паузы, и рвать соединение
        # каждые 5 секунд означает переустанавливать его сотни раз за файл.
        timeout_keep_alive=120,
        # Заголовки у нас короткие; запас нужен на длинные имена файлов в
        # Content-Disposition, но не на мегабайты — это защита от мусора.
        h11_max_incomplete_event_size=64 * 1024,
        # Служба не должна висеть вечно при остановке, если кто-то качает
        # пятигигабайтный файл: NSSM иначе прибьёт её жёстко.
        timeout_graceful_shutdown=30,
        # Перезапуск воркера по числу запросов недопустим: он оборвёт
        # активные передачи на середине.
        limit_max_requests=None,
    )
    return 0


def configure_output() -> None:
    """Вывод не должен падать из-за кодировки консоли.

    Когда stdout перенаправлен в файл или канал, Python берёт кодировку локали:
    на английской Windows это cp1252, где кириллицы нет вовсе, и первый же print
    с русским текстом роняет команду целиком. Так падает установщик, читающий
    вывод `init`, и служба под NSSM, пишущая stdout в журнал.

    Настоящая консоль Unicode умеет сама, там достаточно подстраховки errors.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Кодировку сменить не удалось — работаем как есть, но не падаем.
            pass


def main(argv: list[str] | None = None) -> int:
    configure_output()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
