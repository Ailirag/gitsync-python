"""Источники версий хранилища.

Три реализации:

* :class:`NativeStorageBackend` — настоящий конфигуратор 1С: отчёт по версиям
  (``/ConfigurationRepositoryReport``), получение версии в изолированную файловую базу
  (``/ConfigurationRepositoryUpdateCfg -v N``) и выгрузка в файлы (``/DumpConfigToFiles``).
  Каждый рабочий поток получает **свою** временную базу и свой каталог выгрузки.
* :class:`FixtureStorageBackend` — ЯВНО ПОМЕЧЕННЫЙ файловый бэкенд для герметичных тестов:
  читает заранее снятый отчёт и каталоги ``v<номер>``. Ничего не эмулирует сверх этого.
* :class:`FakeStorageBackend` — только для автотестов конвейера (задержки, сбои, гонки).
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import threading
import time
import uuid
from importlib.resources import as_file, files
from pathlib import Path
from typing import Protocol

from .designer import DesignerRunner, StorageAccess
from .errors import CancelledError, GitSyncError, UnsafePathError
from .locks import exclusive_lock
from .repository_session import repository_session_path
from .safepath import (
    Identity,
    directory_identity,
    discard_owned_directory,
    require_private_scratch,
    safe_join,
)
from .storage_report import StorageVersion, parse_storage_report

log = logging.getLogger("gitsync.backend")


class StorageBackend(Protocol):
    """Контракт источника версий."""

    def fetch_history(self, begin: int = 1) -> list[StorageVersion]:
        ...

    def export_version(self, version: int, dest: Path, cancel: threading.Event | None = None) -> None:
        ...


def _keeps_diagnostics_on_failure(method):
    """Отказ сохраняет протоколы конфигуратора; успех и отмена — нет.

    Причину отказа конфигуратор пишет ТОЛЬКО в файл ``/Out``, и текст ошибки ссылается
    на путь этого файла («откройте протокол конфигуратора» — docs/setup-guide.md,
    docs/docker-guide.md). Удалить его вместе с одноразовым состоянием значило бы
    оборвать документированный разбор отказа. Отмена по запросу оператора отказом не
    является: иначе каждая штатная остановка оставляла бы каталог на постоянном томе.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except CancelledError:
            raise
        except BaseException:
            self._keep_diagnostics = True
            raise
    return wrapper


class NativeStorageBackend:
    """Работа с настоящим хранилищем через конфигуратор 1С."""

    def __init__(
        self,
        access: StorageAccess,
        runner: DesignerRunner,
        temp_root: str | Path,
        extension: str | None = None,
        ib_factory=None,
        owns_temp_root: bool = False,
    ):
        self.access = access
        self._session_path = repository_session_path(access)
        self.runner = runner
        self.temp_root = Path(temp_root)
        self.extension = extension
        # ib_factory(worker_dir) -> строка соединения с ИБ; по умолчанию файловая база.
        self.ib_factory = ib_factory or self._create_file_infobase
        self._local = threading.local()
        #: Рабочие каталоги вместе с удостоверением, снятым в момент создания: удаляют по
        #: удостоверению, а не по пути — путь к моменту очистки может вести куда угодно.
        self._worker_dirs: list[tuple[Path, Identity | None]] = []
        #: Каталог запуска принадлежит ЭТОМУ запуску и снимается вместе с ним. Владение
        #: подтверждается созданием, а не именем: каталог, переданный снаружи
        #: (``owns_temp_root=False``), может быть общим или постоянным, и его не удаляют.
        self.owns_temp_root = bool(owns_temp_root)
        #: Каталог запуска ещё не создан: его создаёт первая настоящая работа, а не
        #: построение бэкенда. Иначе прогон, упавший на подготовке репозитория (git
        #: отказал, небезопасный путь, занятая цель), оставлял бы пустой каталог —
        #: тот же неограниченный рост на постоянном томе, только инодами.
        self._scratch_created = False
        #: Удостоверение созданного каталога запуска и замок его первого создания:
        #: «проверить флаг» и «создать каталог» — две операции, и одновременное первое
        #: обращение нескольких потоков иначе отказывает всем, кроме одного.
        self._scratch_identity: Identity | None = None
        #: Рекурсивный: ``_worker_context`` держит замок и сам вызывает ``_ensure_scratch_root``.
        self._scratch_lock = threading.RLock()
        #: Диагностика отказа пережила очистку — каталог запуска оставлен намеренно.
        self._keep_diagnostics = False
        if access.password:
            # Конфигуратор повторяет свои аргументы в сообщениях — вымарываем значение всюду.
            self.runner.secrets.append(access.password)

    def _ensure_scratch_root(self) -> None:
        """Готовит корень временных каталогов перед первой работой в нём.

        Для собственного каталога запуска ``exist_ok=False`` — владение подтверждается
        созданием: удалять можно только то, что создали сами. Создание идёт под замком:
        первое обращение нескольких потоков сразу иначе даёт ``FileExistsError`` у всех,
        кроме одного, — сам каталог при этом создан, и отказ получают работающие потоки.
        Чужой (внешний или постоянный) корень лишь дополняется, но владельцем бэкенд не
        становится.
        """
        # Договор контейнерной поставки (stage-18): общий том под каталогом запуска —
        # отказ ДО создания и ДО любой уборки. Проверяются оба режима: и свой каталог,
        # и переданный снаружи постоянный корень.
        require_private_scratch(self.temp_root, "Каталог запуска бэкенда")
        if not self.owns_temp_root:
            self.temp_root.mkdir(parents=True, exist_ok=True)
            return
        with self._scratch_lock:
            if self._scratch_created:
                return
            self.temp_root.mkdir(parents=True, exist_ok=False)
            # Удостоверение снимается сразу после создания: позже путь можно подменить,
            # а объект файловой системы — нет.
            self._scratch_identity = directory_identity(self.temp_root)
            self._scratch_created = True

    def _create_file_infobase(self, worker_dir: Path) -> str:
        """Создаёт пустую файловую базу для потока.

        Строка соединения передаётся БЕЗ кавычек: ``CREATEINFOBASE File=<путь>``. Кавычки в
        документации относятся к разбору командной строки оболочкой, а мы запускаем процесс
        argv-массивом без shell. Проверено на 8.3.27.2130: ``File="<путь>"`` даёт код 1 и не
        создаёт базу, ``File=<путь>`` — код 0 и настоящий файл базы.

        Наличие базы проверяется по артефакту ``1Cv8.1CD`` — нулевой код возврата сам по себе
        недостаточен. Если база создана снаружи, передайте строку соединения через ``ib_factory``.

        ``/Out`` обязателен и здесь: причину отказа конфигуратор пишет только в этот файл,
        а это самый первый его запуск — без файла отказ на старте остался бы без объяснения.
        """
        base_dir = worker_dir / "ib"
        base_dir.mkdir(parents=True, exist_ok=True)
        self.runner.run(
            [
                self.runner.v8_path,
                "CREATEINFOBASE",
                f"File={base_dir}",
                "/DisableStartupDialogs",
                "/DisableStartupMessages",
                "/Out",
                str(self.runner.out_file()),
            ]
        )
        if not (base_dir / "1Cv8.1CD").is_file():
            raise GitSyncError(f"CREATEINFOBASE не создал файловую базу: {base_dir}")
        return f"/F{base_dir}"

    def _worker_context(self) -> tuple[Path, str]:
        """Контекст потока: собственный каталог с уникальным именем и собственная ИБ.

        Имя каталога — UUID, а не идентификатор потока: идентификаторы переиспользуются
        после завершения потока, и два прогона могли бы попасть в один каталог.
        """
        context = getattr(self._local, "context", None)
        if context is None:
            # Корень, свой каталог и запись о нём — под одним замком: иначе очистка,
            # начатая между созданием каталога и записью о нём, снимает каталог и
            # оставляет о нём запись, а поток продолжает работать в уже снятом каталоге.
            # Создание ИБ (запуск конфигуратора) в замок не входит: оно долгое и чужим
            # каталогам не мешает.
            with self._scratch_lock:
                self._ensure_scratch_root()
                worker_dir = self.temp_root / f"worker-{uuid.uuid4().hex}"
                worker_dir.mkdir(parents=True, exist_ok=False)
                self._worker_dirs.append((worker_dir, directory_identity(worker_dir)))
            context = (worker_dir, self.ib_factory(worker_dir))
            if self.extension:
                # Upstream СоздатьРасширениеВБазе: generic empty extension, not
                # a repository fixture. Load before *any* repository operation.
                template = files("gitsync").joinpath("data/tempExtension.cfe")
                with as_file(template) as seed:
                    self.runner.run(self.runner.build_load_cfg_args(
                        seed, context[1], extension=self.extension
                    ))
            self._local.context = context
        return context

    def cleanup(self) -> None:
        """Удаляет ТОЛЬКО собственные каталоги — и только пока они остаются своими.

        Одноразовое состояние (временные ИБ, полученные ``.cf``, отчёт) снимается всегда.
        Каталог запуска целиком удаляется, только если его создал этот экземпляр и в нём
        не осталось диагностики отказа. Общий родитель временных файлов не трогается:
        им пользуются другие репозитории и параллельные запуски.

        Удостоверение каталога запуска проверяется ДО удаления чего-либо внутри него:
        рабочие каталоги лежат внутри, и по подменённому корню путь к каждому из них
        ведёт уже в чужое дерево — подмену ПРЕДКА защита ``rmtree`` от ссылки на самом
        удаляемом каталоге не ловит. Потеря владения — отказ от очистки: каталоги
        остаются в списке, и следующая очистка проверит их заново.

        Вызывать полагается ПОСЛЕ остановки пула: метод освобождает ресурсы всего
        экземпляра, а не текущего потока, и одновременная работа другого потока в том же
        каталоге запуска — не поддерживаемый порядок. Замок здесь не заменяет этого
        порядка, а исключает разрыв учёта: пока очистка идёт, новый рабочий каталог не
        может быть создан наполовину — созданным, но не записанным, или записанным, но
        уже снятым.

        Отклонённый рабочий каталог ЗАЩИЩАЕТСЯ от последующей уборки корня (stage-14,
        S4b). Раньше отказ по удостоверению касался только самого каталога, а затем
        рекурсивное снятие каталога запуска уносило подменённое содержимое вместе со всем
        прочим: признали объект чужим — и тут же уничтожили. Теперь имя такого каталога
        передаётся в ``protect``, корень остаётся непустым, и его снятие честно не
        состоится.
        """
        with self._scratch_lock:
            pending, self._worker_dirs = self._worker_dirs, []
            self._local = threading.local()
            if self._owned_root_lost():
                self._worker_dirs = pending
                return
            refused: set[str] = set()
            for worker_dir, identity in pending:
                # stage-15, EVA-13: если вернуть перемещённое на исходное имя не удалось
                # (оно занято), объект остаётся под временным именем изоляции. Это имя
                # тоже обязано попасть под защиту: иначе уборка корня снесёт то, что мы
                # только что отказались трогать.
                left_behind: set[str] = set()
                if not discard_owned_directory(worker_dir, identity, "Рабочий каталог",
                                               displaced=left_behind):
                    self._worker_dirs.append((worker_dir, identity))
                    # Защищается ИМЯ внутри каталога запуска: снимать его нельзя ни
                    # самому, ни заодно с родителем.
                    if worker_dir.parent == self.temp_root:
                        refused.add(worker_dir.name)
                if worker_dir.parent == self.temp_root:
                    refused |= left_behind
            self._discard_scratch_root(refused)

    def _owned_root_lost(self) -> bool:
        """Каталог запуска перестал быть тем объектом, который создал этот запуск.

        Исчезнувший каталог подменой не считается: внутри него удалять уже нечего, и
        повторная очистка не имеет права тревожить журнал из-за прибранного тома.
        """
        if not self.owns_temp_root or not self._scratch_created:
            return False
        try:
            os.lstat(self.temp_root)
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("Каталог запуска прочитать не удалось, очистка отменена: %s (%s)",
                        self.temp_root, exc)
            return True
        if self._scratch_identity is not None \
                and directory_identity(self.temp_root) == self._scratch_identity:
            return False
        log.warning("Каталог запуска перестал быть своим, очистка отменена: %s", self.temp_root)
        return True

    def _discard_scratch_root(self, protect: set[str] | None = None) -> None:
        """Снимает собственный каталог запуска, если он больше ничего не значит.

        ``protect`` — имена, принадлежность которых уже отклонена: их не снимает ни эта
        уборка, ни рекурсия внутри неё.
        """
        if not self.owns_temp_root or not self._scratch_created:
            return
        if self._keep_diagnostics:
            log.info("Протоколы конфигуратора оставлены для разбора отказа: %s", self.temp_root)
            return
        if protect:
            log.warning("Каталог запуска не снимается целиком: внутри осталось отклонённое "
                        "по удостоверению содержимое (%s): %s",
                        ", ".join(sorted(protect)), self.temp_root)
        if not discard_owned_directory(self.temp_root, self._scratch_identity,
                                       "Каталог запуска", protect):
            return
        # Каталог снят — владение придётся подтверждать заново, если бэкенд ещё используют.
        self._scratch_created = False
        self._scratch_identity = None

    @_keeps_diagnostics_on_failure
    def fetch_history(self, begin: int = 1) -> list[StorageVersion]:
        worker_dir, ib_connection = self._worker_context()
        # Отчёт конфигуратора — табличный документ (MOXCEL) независимо от расширения файла.
        report_path = worker_dir / f"storage-report-{uuid.uuid4().hex}.mxl"
        args = self.runner.build_report_args(
            self.access, report_path, begin=max(begin, 1), ib_connection=ib_connection,
            extension=self.extension,
        )
        with exclusive_lock(self._session_path, timeout=self.runner.timeout):
            self.runner.run(args)
        if not report_path.is_file() or report_path.stat().st_size == 0:
            raise GitSyncError(f"Конфигуратор не создал отчёт по версиям: {report_path}")
        return parse_storage_report(report_path.read_bytes())

    @_keeps_diagnostics_on_failure
    def export_version(self, version: int, dest: Path, cancel: threading.Event | None = None) -> None:
        """Версия хранилища → каталог XML.

        Три ОТДЕЛЬНЫХ запуска конфигуратора, как проверено на стенде:
        ``/ConfigurationRepositoryDumpCfg -v N`` → ``/LoadCfg`` → ``/DumpConfigToFiles``.
        Совмещение LoadCfg и DumpConfigToFiles в одном запуске давало код 0 без XML,
        поэтому объединять их нельзя. Каждый шаг проверяется по артефакту.
        """
        if cancel is not None and cancel.is_set():
            raise CancelledError("Отменено до начала выгрузки версии")
        worker_dir, ib_connection = self._worker_context()
        dest.mkdir(parents=True, exist_ok=True)

        suffix = ".cfe" if self.extension else ".cf"
        cf_path = worker_dir / f"v{version}-{uuid.uuid4().hex}{suffix}"
        # Same-login native sessions conflict even across separate private IBs.
        # Hold the OS lock only until RepositoryDumpCfg exits, NOT through LoadCfg/XML.
        with exclusive_lock(self._session_path, timeout=self.runner.timeout):
            if cancel is not None and cancel.is_set():
                raise CancelledError("Отменено при ожидании сессии хранилища")
            self.runner.run(self.runner.build_dump_cfg_args(
                self.access, version, cf_path, ib_connection, extension=self.extension
            ))
        if not cf_path.is_file() or cf_path.stat().st_size == 0:
            raise GitSyncError(f"Конфигуратор не выгрузил версию {version} из хранилища: {cf_path}")

        if cancel is not None and cancel.is_set():
            raise CancelledError("Отменено после получения версии из хранилища")

        self.runner.run(
            self.runner.build_load_cfg_args(cf_path, ib_connection, extension=self.extension)
        )
        if cancel is not None and cancel.is_set():
            raise CancelledError("Отменено после загрузки версии во временную базу")

        self.runner.run(self.runner.build_dump_args(dest, ib_connection, extension=self.extension))
        if not any(dest.iterdir()):
            raise GitSyncError(
                f"Конфигуратор не выгрузил версию {version} в файлы: каталог {dest} пуст"
            )
        cf_path.unlink(missing_ok=True)


class FixtureStorageBackend:
    """Файловый бэкенд для герметичных прогонов. НЕ является заменой платформы 1С.

    Ожидаемая раскладка::

        <root>/report.mxl        # отчёт /ConfigurationRepositoryReport (MOXCEL)
        <root>/v1/...            # содержимое выгрузки версии 1
        <root>/v2/...

    Имя ``report.txt`` тоже принимается: конфигуратор пишет MOXCEL независимо от расширения.
    """

    is_fixture = True
    REPORT_NAMES = ("report.mxl", "report.txt")

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def fetch_history(self, begin: int = 1) -> list[StorageVersion]:
        report = next((self.root / name for name in self.REPORT_NAMES if (self.root / name).is_file()), None)
        if report is None:
            raise GitSyncError(
                f"Не найден файл отчёта фикстуры: {self.root / self.REPORT_NAMES[0]}"
            )
        versions = parse_storage_report(report.read_bytes())
        return [item for item in versions if item.number >= begin]

    def export_version(self, version: int, dest: Path, cancel: threading.Event | None = None) -> None:
        source = self.root / f"v{version}"
        if not source.is_dir():
            raise GitSyncError(f"В фикстуре нет каталога выгрузки для версии {version}: {source}")
        dest.mkdir(parents=True, exist_ok=True)
        for item in sorted(source.rglob("*")):
            if item.is_symlink():
                raise UnsafePathError(f"Фикстура содержит символьную ссылку: {item}")
            target = safe_join(dest, item.relative_to(source))
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, target)


class FakeStorageBackend:
    """Управляемый бэкенд для автотестов конвейера (задержки, сбои, наблюдение за гонками)."""

    is_fixture = True

    def __init__(
        self,
        versions: list[StorageVersion],
        delays: dict[int, float] | None = None,
        fail_versions: dict[int, Exception] | None = None,
        flaky_versions: dict[int, int] | None = None,
    ):
        self.versions = list(versions)
        self.delays = delays or {}
        self.fail_versions = fail_versions or {}
        self.flaky_versions = dict(flaky_versions or {})
        self.files: dict[int, dict[str, str]] = {}
        self.symlinks: dict[int, dict[str, str]] = {}

        self.attempts: dict[int, int] = {}
        self.export_dirs: dict[int, str] = {}
        self.completion_order: list[int] = []
        self.max_concurrent = 0
        self.max_inflight = 0
        self._active = 0
        self._inflight = 0
        self._lock = threading.Lock()

    def fetch_history(self, begin: int = 1) -> list[StorageVersion]:
        return [item for item in self.versions if item.number >= begin]

    def export_version(self, version: int, dest: Path, cancel: threading.Event | None = None) -> None:
        with self._lock:
            self.attempts[version] = self.attempts.get(version, 0) + 1
            self._active += 1
            self._inflight += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
            self.max_inflight = max(self.max_inflight, self._inflight)
            attempt = self.attempts[version]
        try:
            delay = self.delays.get(version, 0.0)
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    raise CancelledError(f"Выгрузка версии {version} отменена")
                time.sleep(0.01)
            if cancel is not None and cancel.is_set():
                raise CancelledError(f"Выгрузка версии {version} отменена")

            if version in self.fail_versions:
                raise self.fail_versions[version]
            if self.flaky_versions.get(version, 0) >= attempt:
                raise GitSyncError(f"Временный сбой выгрузки версии {version}, попытка {attempt}")

            dest.mkdir(parents=True, exist_ok=True)
            for name, content in self.files.get(version, {"Конфигурация.xml": f"версия {version}"}).items():
                target = safe_join(dest, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            for name, target_path in self.symlinks.get(version, {}).items():
                link = safe_join(dest, name)
                try:
                    link.symlink_to(target_path)
                except (OSError, NotImplementedError):
                    # Без прав на симлинки в Windows — кладём обычный файл-заглушку.
                    link.write_text("нет прав на симлинк", encoding="utf-8")
            with self._lock:
                self.export_dirs[version] = str(dest)
                self.completion_order.append(version)
        finally:
            with self._lock:
                self._active -= 1
                self._inflight -= 1
