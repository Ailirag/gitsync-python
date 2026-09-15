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

import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Protocol

from .designer import DesignerRunner, StorageAccess
from .errors import CancelledError, GitSyncError, UnsafePathError
from .safepath import safe_join
from .storage_report import StorageVersion, parse_storage_report

log = logging.getLogger("gitsync.backend")


class StorageBackend(Protocol):
    """Контракт источника версий."""

    def fetch_history(self, begin: int = 1) -> list[StorageVersion]:
        ...

    def export_version(self, version: int, dest: Path, cancel: threading.Event | None = None) -> None:
        ...


class NativeStorageBackend:
    """Работа с настоящим хранилищем через конфигуратор 1С."""

    def __init__(
        self,
        access: StorageAccess,
        runner: DesignerRunner,
        temp_root: str | Path,
        extension: str | None = None,
        ib_factory=None,
    ):
        self.access = access
        self.runner = runner
        self.temp_root = Path(temp_root)
        self.extension = extension
        # ib_factory(worker_dir) -> строка соединения с ИБ; по умолчанию файловая база.
        self.ib_factory = ib_factory or self._create_file_infobase
        self._local = threading.local()

    def _create_file_infobase(self, worker_dir: Path) -> str:
        """Создаёт пустую файловую базу для потока.

        Строка соединения передаётся БЕЗ кавычек: ``CREATEINFOBASE File=<путь>``. Кавычки в
        документации относятся к разбору командной строки оболочкой, а мы запускаем процесс
        argv-массивом без shell. Проверено на 8.3.27.2130: ``File="<путь>"`` даёт код 1 и не
        создаёт базу, ``File=<путь>`` — код 0 и настоящий файл базы.

        Наличие базы проверяется по артефакту ``1Cv8.1CD`` — нулевой код возврата сам по себе
        недостаточен. Если база создана снаружи, передайте строку соединения через ``ib_factory``.
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
            worker_dir = self.temp_root / f"worker-{uuid.uuid4().hex}"
            worker_dir.mkdir(parents=True, exist_ok=False)
            context = (worker_dir, self.ib_factory(worker_dir))
            self._local.context = context
        return context

    def fetch_history(self, begin: int = 1) -> list[StorageVersion]:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        worker_dir, ib_connection = self._worker_context()
        # Отчёт конфигуратора — табличный документ (MOXCEL) независимо от расширения файла.
        report_path = worker_dir / f"storage-report-{uuid.uuid4().hex}.mxl"
        args = self.runner.build_report_args(
            self.access, report_path, begin=max(begin, 1), ib_connection=ib_connection
        )
        self.runner.run(args)
        if not report_path.is_file() or report_path.stat().st_size == 0:
            raise GitSyncError(f"Конфигуратор не создал отчёт по версиям: {report_path}")
        return parse_storage_report(report_path.read_bytes())

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

        cf_path = worker_dir / f"v{version}-{uuid.uuid4().hex}.cf"
        self.runner.run(self.runner.build_dump_cfg_args(self.access, version, cf_path, ib_connection))
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
