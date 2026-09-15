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

        НЕ ВЕРИФИЦИРОВАНО на живой платформе: используется документированный ключ
        ``CREATEINFOBASE File="<путь>"``. Если база уже создана снаружи, передайте
        готовую строку соединения через ``ib_factory``.
        """
        base_dir = worker_dir / "ib"
        base_dir.mkdir(parents=True, exist_ok=True)
        self.runner.run(
            [self.runner.v8_path, "CREATEINFOBASE", f'File="{base_dir}"', "/DisableStartupDialogs"]
        )
        return f"/F{base_dir}"

    def _worker_context(self) -> tuple[Path, str]:
        context = getattr(self._local, "context", None)
        if context is None:
            worker_dir = self.temp_root / f"worker-{threading.get_ident()}"
            worker_dir.mkdir(parents=True, exist_ok=True)
            context = (worker_dir, self.ib_factory(worker_dir))
            self._local.context = context
        return context

    def fetch_history(self, begin: int = 1) -> list[StorageVersion]:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        report_path = self.temp_root / "storage-report.txt"
        worker_dir, ib_connection = self._worker_context()
        args = self.runner.build_report_args(
            self.access, report_path, begin=max(begin, 1), ib_connection=ib_connection
        )
        self.runner.run(args)
        if not report_path.is_file():
            raise GitSyncError(f"Конфигуратор не создал отчёт по версиям: {report_path}")
        # Конфигуратор пишет отчёт в UTF-8 (реже — в UTF-16 с BOM); читаем терпимо.
        raw = report_path.read_bytes()
        is_utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
        text = raw.decode("utf-16") if is_utf16 else raw.decode("utf-8", "replace")
        return parse_storage_report(text)

    def export_version(self, version: int, dest: Path, cancel: threading.Event | None = None) -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledError("Отменено до начала выгрузки версии")
        worker_dir, ib_connection = self._worker_context()
        dest.mkdir(parents=True, exist_ok=True)
        self.runner.run(self.runner.build_update_cfg_args(self.access, version, ib_connection))
        if cancel is not None and cancel.is_set():
            raise CancelledError("Отменено после получения версии из хранилища")
        self.runner.run(self.runner.build_dump_args(dest, ib_connection, extension=self.extension))


class FixtureStorageBackend:
    """Файловый бэкенд для герметичных прогонов. НЕ является заменой платформы 1С.

    Ожидаемая раскладка::

        <root>/report.txt        # отчёт /ConfigurationRepositoryReport
        <root>/v1/...            # содержимое выгрузки версии 1
        <root>/v2/...
    """

    is_fixture = True

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def fetch_history(self, begin: int = 1) -> list[StorageVersion]:
        report = self.root / "report.txt"
        if not report.is_file():
            raise GitSyncError(f"Не найден файл отчёта фикстуры: {report}")
        versions = parse_storage_report(report.read_text(encoding="utf-8-sig"))
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
