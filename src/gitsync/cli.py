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
import uuid
from pathlib import Path

from . import __version__
from .backends import FixtureStorageBackend, NativeStorageBackend
from .designer import DEFAULT_DESIGNER_TIMEOUT, DesignerRunner, StorageAccess
from .errors import ConfigError, GitSyncError
from .gitrepo import GitRepo
from .plugins import PluginHost
from .safepath import default_scratch_root
from .sync import SyncManager, SyncOptions, discover_repo_root
from .transaction import owned_path

log = logging.getLogger("gitsync.cli")

#: Код возврата «остановлено по запросу оператора» (общепринятый 128 + SIGINT).
#: Планировщик и `docker stop` обязаны отличать штатную остановку от сбоя.
EXIT_CANCELLED = 130


def reject_temp_inside_repo(temp_root: Path, repo_root: Path) -> None:
    """Временные файлы внутри рабочего дерева — мусор в чужом репозитории и риск очистки."""
    temp = temp_root.resolve() if temp_root.exists() else temp_root.absolute()
    repo = repo_root.resolve() if repo_root.exists() else repo_root.absolute()
    if temp == repo or repo in temp.parents:
        raise ConfigError(
            f"Каталог временных файлов <{temp_root}> находится внутри рабочей копии <{repo_root}>. "
            "Укажите --temp-root вне репозитория."
        )


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
            "Для нативного бэкенда укажите --v8-path — полный путь к исполняемому файлу "
            "конфигуратора: в Windows это ...\\1cv8\\<версия>\\bin\\1cv8.exe, в Linux — "
            "/opt/1cv8/x86_64/<версия>/1cv8 (без .exe, имя чувствительно к регистру). "
            "Для герметичного прогона используйте --backend fixture."
        )
    access = build_storage_access(
        storage_path=args.storage_path or "",
        user=args.storage_user or "",
        password_env=args.storage_password_env,
        password_file=args.storage_password_file,
    )
    # Собственный каталог запуска: общий родитель могут использовать другие репозитории
    # и параллельные запуски, а рабочие каталоги конфигуратора нельзя делить.
    # Отсчёт идёт от НАСТОЯЩЕГО корня репозитория: у источника общего репозитория базы
    # родитель рабочего каталога — сам репозиторий, и выгрузка конфигуратора оказалась бы
    # внутри чужого рабочего дерева (untracked-мусор для соседних источников).
    repo_root = discover_repo_root(args.workdir)
    temp_root = Path(args.temp_root or default_scratch_root(repo_root.parent / ".gitsync-tmp"))
    reject_temp_inside_repo(temp_root, repo_root)
    temp_root = temp_root / f"native-{uuid.uuid4().hex}"
    runner = DesignerRunner(
        v8_path=args.v8_path,
        out_dir=temp_root / "designer-out",
        version=args.v8_version or "",
        timeout=args.designer_timeout,
    )
    # Каталог `native-<uuid>` создаёт и снимает сам бэкенд: он одноразовый и принадлежит
    # этому запуску, в отличие от общего родителя, указанного оператором.
    return NativeStorageBackend(
        access=access, runner=runner, temp_root=temp_root, extension=args.extension,
        owns_temp_root=True,
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
        disable_auto_src=getattr(args, "disable_auto_src", False),
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
    manager.init_working_copy(generate_authors=not args.no_authors, raise_on_error=True)
    print(f"Рабочая копия подготовлена: {args.workdir}")
    return 0


def _cmd_sync(args) -> int:
    manager = _make_manager(args)
    cancel = _install_cancellation()
    result = manager.sync(cancel=cancel, raise_on_error=False)
    if result.committed:
        print(f"Зафиксировано версий: {len(result.committed)} "
              f"({result.committed[0]}..{result.committed[-1]})")
    elif result.error is None:
        # «Новых версий нет» — утверждение о ХРАНИЛИЩЕ, и после отказа его сделать нельзя:
        # прогон, упавший на отчёте (например, платформа не нашла лицензию), про версии
        # не узнал ничего. Печатать это рядом с ошибкой значит противоречить самому себе.
        print("Новых версий нет")
    else:
        print("Версии не синхронизированы: прогон завершился ошибкой")
    if result.cancelled:
        print("Синхронизация остановлена по запросу отмены")
        return EXIT_CANCELLED
    if result.error is not None:
        # Отказ подготовки (занятая цель, грязная копия, маркер) не привязан к версии:
        # «Ошибка на версии None» сбивала бы с толку.
        if result.failed_version is None:
            print(f"Ошибка: {result.error}", file=sys.stderr)
        else:
            print(f"Ошибка на версии {result.failed_version}: {result.error}", file=sys.stderr)
        return 1
    return 0


def _cmd_clone(args) -> int:
    GitRepo(args.workdir).clone(args.url)
    return _cmd_init(args)


def _cmd_set_version(args) -> int:
    manager = SyncManager(args.workdir, None, SyncOptions(
        disable_auto_src=args.disable_auto_src, lock_timeout=args.lock_timeout))
    manager.set_version(args.version, commit=args.commit, author=args.commit_author,
                        raise_on_error=True)
    print(f"Версия {args.version} записана")
    return 0


#: Ключи, которые понимает пакетный режим. Неизвестная схема (например, upstream
#: ``repositories``) отвергается явно: молча выполнить ноль хранилищ и вернуть 0 нельзя.
_BATCH_TOP_KEYS = frozenset({"storages", "defaults", "repository"})
_BATCH_ENTRY_KEYS = frozenset({
    "name", "disable", "disabled", "workdir", "subtree", "init", "no_authors", "backend",
    "fixture_root", "storage_path",
    "storage_user", "storage_password_env", "storage_password_file", "v8_path", "v8_version",
    "extension", "designer_timeout", "jobs", "queue_limit", "retries", "temp_root",
    "email_domain", "keep_temp", "lock_timeout", "limit", "allow_dirty", "disable_auto_src",
    "plugins",
})


def _resolve_subtree(repository: str, subtree: object, position: int) -> str:
    """Подкаталог источника внутри общего репозитория: только относительный безопасный путь."""
    if not isinstance(subtree, str) or not subtree.strip():
        raise ConfigError(f"Хранилище №{position}: <subtree> должен быть непустой строкой")
    normalized = subtree.replace("\\", "/").strip("/")
    if not normalized:
        raise ConfigError(f"Хранилище №{position}: <subtree> не может указывать на сам репозиторий")
    try:
        target = owned_path(Path(repository), normalized)
    except GitSyncError as exc:
        raise ConfigError(f"Хранилище №{position}: недопустимый <subtree> <{subtree}>: {exc}") from exc
    return str(target)


def _reject_overlapping_workdirs(entries: list[dict]) -> None:
    """Перекрытие рабочих каталогов = молчаливое уничтожение соседнего источника."""
    seen: list[tuple[str, str, str]] = []
    for entry in entries:
        path = os.path.normpath(str(Path(entry["workdir"]).absolute()))
        # Сравнение без учёта регистра — как на NTFS и на подключённом томе Windows.
        # На ext4 «Расширение» и «расширение» это два каталога, а на машине разработчика
        # они схлопнутся в один: историю такого репозитория нельзя развернуть везде.
        folded = path.lower()
        for other_path, other_folded, other_name in seen:
            if path == other_path:
                raise ConfigError(
                    f"Хранилища <{other_name}> и <{entry['name']}> используют один каталог "
                    f"<{entry['workdir']}>: у каждого источника должен быть свой подкаталог."
                )
            if folded == other_folded:
                raise ConfigError(
                    f"Каталоги хранилищ <{other_name}> и <{entry['name']}> различаются только "
                    f"регистром букв (<{entry['workdir']}>). В Linux это разные каталоги, а в "
                    "Windows и на подключённом томе Windows — один и тот же: рабочая копия "
                    "такого репозитория соберётся не на всякой машине. Дайте источникам "
                    "подкаталоги с разными именами, а не с разным регистром."
                )
            if (folded.startswith(other_folded + os.sep)
                    or other_folded.startswith(folded + os.sep)):
                raise ConfigError(
                    f"Каталоги хранилищ <{other_name}> и <{entry['name']}> вложены друг в друга "
                    f"(<{entry['workdir']}>): источники должны лежать в непересекающихся подкаталогах."
                )
        seen.append((path, folded, entry["name"]))


def _validate_batch_config(config: object) -> list[dict]:
    """Проверяет схему ДО любых работ и возвращает список записей."""
    if not isinstance(config, dict):
        raise ConfigError("Файл конфигурации должен содержать объект JSON с ключом <storages>")
    unknown = sorted(set(config) - _BATCH_TOP_KEYS)
    if unknown:
        raise ConfigError(
            f"Неизвестные ключи конфигурации: {', '.join(unknown)}. "
            f"Поддерживаются: {', '.join(sorted(_BATCH_TOP_KEYS))}. "
            "Формат конфигурации upstream (ключ <repositories>) не поддерживается — "
            "перечислите хранилища в <storages>."
        )
    if "storages" not in config:
        raise ConfigError("В конфигурации нет ключа <storages>")
    storages = config["storages"]
    if not isinstance(storages, list) or not storages:
        raise ConfigError("Ключ <storages> должен быть непустым списком хранилищ")
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("Ключ <defaults> должен быть объектом")
    repository = config.get("repository")
    if repository is not None and (not isinstance(repository, str) or not repository.strip()):
        raise ConfigError("Ключ <repository> должен быть путём к общему репозиторию Git")
    entries: list[dict] = []
    names: set[str] = set()
    for position, entry in enumerate(storages, 1):
        if not isinstance(entry, dict):
            raise ConfigError(f"Хранилище №{position}: ожидался объект JSON")
        merged = {**defaults, **entry}
        unknown = sorted(set(merged) - _BATCH_ENTRY_KEYS)
        if unknown:
            raise ConfigError(f"Хранилище №{position}: неизвестные ключи {', '.join(unknown)}")
        if merged.get("subtree") and merged.get("workdir"):
            raise ConfigError(
                f"Хранилище №{position}: задайте либо <subtree> (подкаталог общего репозитория), "
                "либо <workdir>, но не оба сразу"
            )
        if merged.get("subtree"):
            if not repository:
                raise ConfigError(
                    f"Хранилище №{position}: <subtree> требует ключа <repository> с путём к "
                    "общему репозиторию Git"
                )
            merged["workdir"] = _resolve_subtree(repository, merged["subtree"], position)
        elif repository and not merged.get("workdir"):
            raise ConfigError(
                f"Хранилище №{position}: при заданном <repository> укажите <subtree> источника"
            )
        if not merged.get("workdir"):
            raise ConfigError(f"Хранилище №{position}: не задан workdir")
        name = str(merged.get("name") or merged["workdir"])
        if name in names:
            raise ConfigError(f"Имя хранилища <{name}> повторяется")
        names.add(name)
        merged["name"] = name
        entries.append(merged)
    _reject_overlapping_workdirs(entries)
    return entries


def _cmd_sync_all(args) -> int:
    try:
        # utf-8-sig, а не utf-8: Windows PowerShell 5.1 (Set-Content/Out-File) пишет UTF-8
        # с BOM, и такой манифест часто переносят на Linux/в контейнер как есть. Без BOM
        # кодек работает как обычный utf-8.
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ConfigError(f"Файл <{args.config}> не является корректным JSON: {exc}") from exc
    entries = _validate_batch_config(config)
    repository = config.get("repository") if isinstance(config, dict) else None
    if repository:
        # Общий репозиторий базы объявлен явно: создаём именно его, а не подкаталоги-источники.
        GitRepo(repository).init()
        print(f"Общий репозиторий: {repository}")
    if args.name:
        selected = [entry for entry in entries if entry["name"] in set(args.name)]
        missing = sorted(set(args.name) - {entry["name"] for entry in entries})
        if missing:
            raise ConfigError(f"В конфигурации нет хранилищ: {', '.join(missing)}")
        entries = selected
    failures: list[str] = []
    executed = 0
    cancelled = False
    for merged in entries:
        name = merged["name"]
        if merged.get("disable") or merged.get("disabled"):
            print(f"=== {name}: отключено в конфигурации, пропускаю ===")
            continue
        executed += 1
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
            disable_auto_src=merged.get("disable_auto_src", False),
            plugin=merged.get("plugins", []),
            no_authors=merged.get("no_authors", False),
            log_level=getattr(args, "log_level", "INFO"),
        )
        try:
            if merged.get("init"):
                _cmd_init(sub)
            code = _cmd_sync(sub)
            if code == EXIT_CANCELLED:
                # Остановку запросил оператор (SIGTERM от `docker stop`, Ctrl+C).
                # Запускать следующее хранилище после этого нельзя: его попросили
                # остановиться, а не «пропустить одно и продолжить».
                cancelled = True
                print(f"Остановлено оператором на хранилище <{name}>; "
                      "следующие хранилища не запускались", file=sys.stderr)
                break
            if code != 0:
                failures.append(str(name))
        except (GitSyncError, OSError) as exc:
            # Пакетный режим не должен падать целиком из-за одного хранилища.
            print(f"Хранилище <{name}>: ошибка {exc}", file=sys.stderr)
            failures.append(str(name))
    if failures:
        # Настоящий сбой важнее остановки: планировщик должен увидеть именно его.
        print(f"Неуспешные хранилища: {', '.join(failures)}", file=sys.stderr)
        return 1
    if cancelled:
        return EXIT_CANCELLED
    if not executed:
        print("Не выполнено ни одного хранилища: все записи отключены или отфильтрованы")
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
        parser.add_argument("--v8-path",
                            help="полный путь к конфигуратору 1С: Windows — 1cv8.exe, "
                                 "Linux — /opt/1cv8/x86_64/<версия>/1cv8")
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
    parser.add_argument("--disable-auto-src", action="store_true",
                        help="не искать подкаталог src: работать строго в --workdir")
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

    clone = sub.add_parser("clone", help="git clone URL + подготовка AUTHORS/VERSION; затем sync")
    _add_common(clone)
    clone.add_argument("--url", required=True, help="URL Git или путь к локальному репозиторию")
    clone.add_argument("--no-authors", action="store_true", help="не генерировать файл AUTHORS")
    clone.set_defaults(func=_cmd_clone)

    sync = sub.add_parser("sync", help="догрузить новые версии хранилища в git")
    _add_common(sync)
    sync.set_defaults(func=_cmd_sync)

    setver = sub.add_parser("set-version", help="записать номер синхронизированной версии в VERSION")
    setver.add_argument("--workdir", required=True, help="каталог рабочей копии git")
    setver.add_argument("--version", dest="version", type=int, required=True, help="номер версии")
    setver.add_argument("--commit", action="store_true", help="сразу зафиксировать VERSION в git")
    setver.add_argument("--commit-author", default="gitsync <gitsync@localhost>",
                        help="автор коммита для --commit")
    setver.add_argument("--disable-auto-src", action="store_true",
                        help="не искать подкаталог src: работать строго в --workdir")
    setver.add_argument("--lock-timeout", type=float, default=5.0,
                        help="ожидание блокировки цели, с (команда оператора — ждём недолго)")
    setver.add_argument("--log-level", default="INFO", help="уровень лога")
    setver.set_defaults(func=_cmd_set_version)

    batch = sub.add_parser("sync-all", help="пакетная синхронизация нескольких хранилищ из JSON-файла")
    batch.add_argument("--config", required=True, help="файл конфигурации со списком хранилищ")
    batch.add_argument("--name", action="append",
                       help="выполнить только указанное хранилище (можно повторять)")
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
