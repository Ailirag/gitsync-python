"""Командная строка gitsync-py (справка на русском).

Пароль хранилища принимается только через переменную окружения или файл: аргумент
командной строки виден в списке процессов, поэтому ``--storage-password`` намеренно
отвергается с подсказкой.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .backends import FixtureStorageBackend, NativeStorageBackend
from .designer import DEFAULT_DESIGNER_TIMEOUT, DesignerRunner, StorageAccess
from .errors import GitSyncError
from .plugins import PluginHost
from .sync import SyncManager, SyncOptions

log = logging.getLogger("gitsync.cli")


def build_storage_access(
    storage_path: str,
    user: str,
    password_env: str | None = None,
    password_file: str | None = None,
) -> StorageAccess:
    password = ""
    if password_env:
        password = os.environ.get(password_env, "")
        if not password:
            raise GitSyncError(f"Переменная окружения <{password_env}> пуста или не задана")
    elif password_file:
        password = Path(password_file).read_text(encoding="utf-8").strip()
    return StorageAccess(path=storage_path, user=user, password=password)


def _build_backend(args) -> object:
    if args.backend == "fixture":
        if not args.fixture_root:
            raise GitSyncError("Для --backend fixture обязателен --fixture-root")
        return FixtureStorageBackend(args.fixture_root)

    if not args.v8_path:
        raise GitSyncError(
            "Для нативного бэкенда укажите --v8-path (полный путь к 1cv8.exe/1cv8). "
            "Для герметичного прогона используйте --backend fixture."
        )
    access = build_storage_access(
        storage_path=args.storage_path or "",
        user=args.storage_user or "",
        password_env=args.storage_password_env,
        password_file=args.storage_password_file,
    )
    temp_root = Path(args.temp_root or (Path(args.workdir).parent / ".gitsync-tmp"))
    runner = DesignerRunner(
        v8_path=args.v8_path,
        out_dir=temp_root / "designer-out",
        version=args.v8_version or "",
        timeout=args.designer_timeout,
    )
    return NativeStorageBackend(
        access=access, runner=runner, temp_root=temp_root, extension=args.extension
    )


def _options_from_args(args) -> SyncOptions:
    return SyncOptions(
        jobs=args.jobs,
        queue_limit=args.queue_limit,
        retries=args.retries,
        temp_root=args.temp_root,
        email_domain=args.email_domain,
        cleanup_temp=not args.keep_temp,
        lock_timeout=args.lock_timeout,
        limit=args.limit,
        allow_dirty=args.allow_dirty,
    )


def _install_cancellation() -> threading.Event:
    cancel = threading.Event()

    def handler(signum, frame):  # noqa: ARG001
        log.warning("Получен сигнал %s — останавливаюсь после текущей версии", signum)
        cancel.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Не главный поток — отмена останется доступной программно.
                pass
    return cancel


def _make_manager(args) -> SyncManager:
    plugins = PluginHost()
    if getattr(args, "plugin", None):
        plugins.load_all(list(args.plugin))
    return SyncManager(args.workdir, _build_backend(args), _options_from_args(args), plugins=plugins)


def _cmd_init(args) -> int:
    manager = _make_manager(args)
    manager.init_working_copy(generate_authors=not args.no_authors)
    print(f"Рабочая копия подготовлена: {args.workdir}")
    return 0


def _cmd_sync(args) -> int:
    manager = _make_manager(args)
    cancel = _install_cancellation()
    result = manager.sync(cancel=cancel, raise_on_error=False)
    if result.committed:
        print(f"Зафиксировано версий: {len(result.committed)} "
              f"({result.committed[0]}..{result.committed[-1]})")
    else:
        print("Новых версий нет")
    if result.cancelled:
        print("Синхронизация остановлена по запросу отмены")
        return 130
    if result.error is not None:
        print(f"Ошибка на версии {result.failed_version}: {result.error}", file=sys.stderr)
        return 1
    return 0


def _cmd_clone(args) -> int:
    manager = _make_manager(args)
    manager.init_working_copy(generate_authors=not args.no_authors)
    return _cmd_sync(args)


def _cmd_set_version(args) -> int:
    from .version_file import write_version_file

    write_version_file(args.workdir, args.version)
    print(f"Версия {args.version} записана в {Path(args.workdir) / 'VERSION'}")
    return 0


def _cmd_sync_all(args) -> int:
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    defaults = config.get("defaults", {})
    failures: list[str] = []
    for entry in config.get("storages", []):
        merged = {**defaults, **entry}
        name = merged.get("name") or merged.get("workdir")
        print(f"=== {name} ===")
        sub = argparse.Namespace(
            workdir=merged["workdir"],
            backend=merged.get("backend", "native"),
            fixture_root=merged.get("fixture_root"),
            storage_path=merged.get("storage_path"),
            storage_user=merged.get("storage_user"),
            storage_password_env=merged.get("storage_password_env"),
            storage_password_file=merged.get("storage_password_file"),
            v8_path=merged.get("v8_path"),
            v8_version=merged.get("v8_version"),
            extension=merged.get("extension"),
            designer_timeout=merged.get("designer_timeout", DEFAULT_DESIGNER_TIMEOUT),
            jobs=merged.get("jobs", 4),
            queue_limit=merged.get("queue_limit", 4),
            retries=merged.get("retries", 1),
            temp_root=merged.get("temp_root"),
            email_domain=merged.get("email_domain", "localhost"),
            keep_temp=merged.get("keep_temp", False),
            lock_timeout=merged.get("lock_timeout", 30.0),
            limit=merged.get("limit"),
            allow_dirty=merged.get("allow_dirty", False),
            plugin=merged.get("plugins", []),
        )
        try:
            code = _cmd_sync(sub)
            if code != 0:
                failures.append(str(name))
        except (GitSyncError, OSError) as exc:
            # Пакетный режим не должен падать целиком из-за одного хранилища.
            print(f"Хранилище <{name}>: ошибка {exc}", file=sys.stderr)
            failures.append(str(name))
    if failures:
        print(f"Неуспешные хранилища: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


def _cmd_plugins(args) -> int:
    host = PluginHost()
    if args.plugin:
        host.load_all(list(args.plugin))
    print("Загруженные плагины:", ", ".join(host.names) or "нет")
    print("Поддерживаемые события:", ", ".join(sorted(host.handlers) or ()) or "—")
    return 0


def _add_common(parser: argparse.ArgumentParser, with_storage: bool = True) -> None:
    parser.add_argument("--workdir", required=True, help="каталог рабочей копии git")
    parser.add_argument("--backend", choices=["native", "fixture"], default="native",
                        help="источник версий: native — конфигуратор 1С, fixture — каталог-фикстура")
    parser.add_argument("--fixture-root", help="каталог фикстуры (report.txt и v<N>/) для --backend fixture")
    if with_storage:
        parser.add_argument("--storage-path", help="путь/строка соединения с хранилищем 1С")
        parser.add_argument("--storage-user", help="пользователь хранилища")
        parser.add_argument("--storage-password-env", help="имя переменной окружения с паролем")
        parser.add_argument("--storage-password-file", help="файл с паролем хранилища")
        parser.add_argument("--storage-password", help=argparse.SUPPRESS)
        parser.add_argument("--v8-path", help="полный путь к исполняемому файлу 1С (1cv8.exe)")
        parser.add_argument("--v8-version", help="версия платформы 1С")
        parser.add_argument("--extension", help="имя расширения для выгрузки (или -AllExtensions)")
        parser.add_argument("--designer-timeout", type=float, default=DEFAULT_DESIGNER_TIMEOUT,
                            help="таймаут одного вызова конфигуратора, с")
    parser.add_argument("--jobs", type=int, default=4, help="сколько версий выгружать параллельно")
    parser.add_argument("--queue-limit", type=int, default=4, help="глубина очереди сверх --jobs")
    parser.add_argument("--retries", type=int, default=1, help="повторов при временном сбое выгрузки")
    parser.add_argument("--temp-root", help="корень временных каталогов")
    parser.add_argument("--email-domain", default="localhost", help="домен почты авторов по умолчанию")
    parser.add_argument("--keep-temp", action="store_true", help="не удалять временные каталоги")
    parser.add_argument("--lock-timeout", type=float, default=30.0, help="ожидание блокировки цели, с")
    parser.add_argument("--limit", type=int, help="обработать не более N версий за запуск")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="разрешить работу с грязной рабочей копией (по умолчанию запрещено)")
    parser.add_argument("--plugin", action="append", help="python-модуль плагина (можно повторять)")
    parser.add_argument("--log-level", default="INFO", help="уровень лога: DEBUG/INFO/WARNING/ERROR")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitsync-py",
        description="Выгрузка версий хранилищ конфигураций 1С в Git с параллельной выгрузкой "
                    "и последовательными коммитами.",
    )
    parser.add_argument("--version", action="version", version=f"gitsync-py {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="команда")

    init = sub.add_parser("init", help="подготовить рабочую копию, создать AUTHORS и VERSION")
    _add_common(init)
    init.add_argument("--no-authors", action="store_true", help="не генерировать файл AUTHORS")
    init.set_defaults(func=_cmd_init)

    clone = sub.add_parser("clone", help="init + полная синхронизация хранилища с нуля")
    _add_common(clone)
    clone.add_argument("--no-authors", action="store_true", help="не генерировать файл AUTHORS")
    clone.set_defaults(func=_cmd_clone)

    sync = sub.add_parser("sync", help="догрузить новые версии хранилища в git")
    _add_common(sync)
    sync.set_defaults(func=_cmd_sync)

    setver = sub.add_parser("set-version", help="записать номер синхронизированной версии в VERSION")
    setver.add_argument("--workdir", required=True, help="каталог рабочей копии git")
    setver.add_argument("--version", dest="version", type=int, required=True, help="номер версии")
    setver.add_argument("--log-level", default="INFO", help="уровень лога")
    setver.set_defaults(func=_cmd_set_version)

    batch = sub.add_parser("sync-all", help="пакетная синхронизация нескольких хранилищ из JSON-файла")
    batch.add_argument("--config", required=True, help="файл конфигурации со списком хранилищ")
    batch.add_argument("--log-level", default="INFO", help="уровень лога")
    batch.set_defaults(func=_cmd_sync_all)

    plugins = sub.add_parser("plugins", help="показать загруженные плагины Python")
    plugins.add_argument("--plugin", action="append", help="python-модуль плагина")
    plugins.add_argument("--log-level", default="INFO", help="уровень лога")
    plugins.set_defaults(func=_cmd_plugins)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if getattr(args, "storage_password", None):
        parser.error(
            "Пароль нельзя передавать в командной строке: он виден в списке процессов. "
            "Используйте --storage-password-env или --storage-password-file."
        )
    try:
        return int(args.func(args))
    except GitSyncError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
