"""Менеджер синхронизации: параллельная выгрузка версий, последовательные коммиты.

Порядок обработки повторяет upstream (``МенеджерСинхронизации.Синхронизировать``):
прочитать номер синхронизированной версии из ``VERSION``, взять историю хранилища,
для каждой следующей версии — получить исходники, очистить рабочую копию (кроме служебных
файлов), перенести выгрузку, записать ``VERSION`` и закоммитить с автором/датой/комментарием.

Отличие: выгрузка версий идёт параллельно в изолированных каталогах, а коммиты выполняются
строго по возрастанию номера версии в одном потоке. Сбой версии N останавливает конвейер
до версии N — версии N+1.. не коммитятся, ``VERSION`` остаётся на последней успешной версии,
поэтому повторный запуск продолжает ровно с места обрыва и не создаёт дублирующих коммитов.
"""

from __future__ import annotations

import logging
import shutil
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .authors import author_signature, read_authors_file
from .errors import (
    CancelledError,
    DirtyWorkingCopyError,
    StorageVersionMismatchError,
    UnsafePathError,
)
from .gitrepo import GitRepo
from .locks import exclusive_lock
from .plugins import PluginHost
from .storage_report import StorageVersion
from .version_file import AUTHORS_FILE_NAME, VERSION_FILE_NAME, read_version_file, write_version_file

log = logging.getLogger("gitsync.sync")

#: Файлы и каталоги рабочей копии, которые не удаляются при очистке (как в upstream).
SERVICE_NAMES = frozenset(
    {".git", ".gitignore", ".gitattributes", ".hooks", AUTHORS_FILE_NAME, VERSION_FILE_NAME}
)

#: Если версия в git больше версии хранилища больше чем на столько — это ошибка (upstream: 10).
DEFAULT_MIN_VERSION_GAP = 10


@dataclass
class SyncOptions:
    jobs: int = 4
    queue_limit: int = 4
    retries: int = 1
    temp_root: Path | str | None = None
    email_domain: str = "localhost"
    min_version_gap: int = DEFAULT_MIN_VERSION_GAP
    cleanup_temp: bool = True
    lock_timeout: float = 30.0
    limit: int | None = None
    allow_dirty: bool = False


@dataclass
class SyncResult:
    committed: list[int] = field(default_factory=list)
    failed_version: int | None = None
    error: BaseException | None = None
    cancelled: bool = False
    max_inflight: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and not self.cancelled


def check_working_copy(repo: GitRepo) -> None:
    """Грязная рабочая копия защищается; служебные файлы gitsync исключение."""
    status = repo.run(["status", "--porcelain", "--untracked-files=all"]).stdout
    dirty: list[str] = []
    for line in status.splitlines():
        entry = line[3:].strip().strip('"')
        if not entry:
            continue
        top = entry.split("/")[0]
        if top in SERVICE_NAMES:
            continue
        dirty.append(entry)
    if dirty:
        raise DirtyWorkingCopyError(
            f"В рабочей копии <{repo.path}> есть чужие незафиксированные изменения: "
            + ", ".join(dirty[:20])
            + "\nЗафиксируйте или уберите их (или используйте --allow-dirty, если это ваши артефакты)."
        )


def validate_export_tree(root: Path) -> None:
    """Проверяет, что выгрузка не выходит за свой каталог и не содержит симлинков."""
    root_resolved = root.resolve()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise UnsafePathError(f"Выгрузка содержит символьную ссылку, это небезопасно: {item}")
        resolved = item.resolve()
        if root_resolved != resolved and root_resolved not in resolved.parents:
            raise UnsafePathError(f"Файл выгрузки вне каталога выгрузки: {item}")


def clean_working_copy(work_dir: Path) -> None:
    for item in work_dir.iterdir():
        if item.name in SERVICE_NAMES:
            continue
        if item.is_symlink() or item.is_file():
            item.unlink()
        else:
            shutil.rmtree(item)


def move_export_into_working_copy(work_dir: Path, export_dir: Path) -> None:
    work_resolved = work_dir.resolve()
    for source in sorted(export_dir.rglob("*")):
        relative = source.relative_to(export_dir)
        target = work_dir / relative
        target_resolved = (work_resolved / relative).parent.resolve()
        if work_resolved != target_resolved and work_resolved not in target_resolved.parents:
            raise UnsafePathError(f"Путь назначения вне рабочей копии: {target}")
        if source.is_dir() and not source.is_symlink():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


class SyncManager:
    """Оркестратор синхронизации одного хранилища с одной рабочей копией."""

    def __init__(self, work_dir: str | Path, backend, options: SyncOptions | None = None,
                 plugins: PluginHost | None = None):
        self.work_dir = Path(work_dir)
        self.backend = backend
        self.options = options or SyncOptions()
        self.repo = GitRepo(self.work_dir)
        self.plugins = plugins or PluginHost()

    # --- вспомогательное -------------------------------------------------

    def _temp_root(self) -> Path:
        root = Path(self.options.temp_root or (self.work_dir.parent / ".gitsync-tmp"))
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _export_one(self, version: StorageVersion, temp_root: Path, cancel: threading.Event) -> Path:
        """Выгружает одну версию в собственный каталог. Повторяет попытки при временных сбоях."""
        attempts = max(self.options.retries, 0) + 1
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            if cancel.is_set():
                raise CancelledError(f"Выгрузка версии {version.number} отменена")
            dest = temp_root / f"v{version.number}-{uuid.uuid4().hex[:8]}"
            try:
                self.backend.export_version(version.number, dest, cancel)
                validate_export_tree(dest)
                return dest
            except (CancelledError, UnsafePathError):
                shutil.rmtree(dest, ignore_errors=True)
                raise
            except BaseException as exc:  # noqa: BLE001 — решение о повторе принимаем ниже
                last_error = exc
                shutil.rmtree(dest, ignore_errors=True)
                log.warning(
                    "Версия %s: попытка %s из %s не удалась: %s", version.number, attempt, attempts, exc
                )
        assert last_error is not None
        raise last_error

    def _commit_version(self, version: StorageVersion, export_dir: Path, authors: dict[str, str]) -> None:
        clean_working_copy(self.work_dir)
        move_export_into_working_copy(self.work_dir, export_dir)
        write_version_file(self.work_dir, version.number)
        signature = author_signature(version.author, authors, self.options.email_domain)
        self.plugins.emit("before_commit", version=version, work_dir=self.work_dir, author=signature)
        sha = self.repo.commit_all(message=version.comment, author=signature, date=version.date)
        self.plugins.emit("after_commit", version=version, work_dir=self.work_dir, sha=sha)
        log.info("Версия %s зафиксирована (%s)", version.number, sha or "без изменений")

    # --- основной сценарий ------------------------------------------------

    def sync(self, cancel: threading.Event | None = None, raise_on_error: bool = True) -> SyncResult:
        cancel = cancel or threading.Event()
        result = SyncResult()

        self.repo.init()
        if not self.options.allow_dirty:
            check_working_copy(self.repo)

        lock_path = self.work_dir / ".git" / "gitsync-py.lock"
        with exclusive_lock(lock_path, timeout=self.options.lock_timeout):
            current = read_version_file(self.work_dir)
            history = sorted(self.backend.fetch_history(current + 1), key=lambda item: item.number)
            self.plugins.emit("after_history", history=history, current_version=current)
            maximum = max((item.number for item in history), default=0)
            if not history and current > 0:
                # История запрашивается от current+1, поэтому пустой ответ ничего не говорит о
                # реальном максимуме: на живом стенде в журнале это «максимум в хранилище: 0».
                # Порог «хранилище пересоздали» нельзя считать по отфильтрованной истории —
                # берём полный отчёт (лишний вызов только когда новых версий нет).
                maximum = max((item.number for item in self.backend.fetch_history(1)), default=0)
            log.info("Синхронизированная версия: %s, максимум в хранилище: %s", current, maximum)

            if current + 1 > maximum and (current + 1 - maximum) > self.options.min_version_gap:
                raise StorageVersionMismatchError(
                    f"Версия в git ({current}) больше версии хранилища ({maximum}) на "
                    f"{current + 1 - maximum}. Возможно, хранилище пересоздали или обрезали. "
                    f"Исправьте файл {VERSION_FILE_NAME} в корне репозитория."
                )

            pending = [item for item in history if item.number > current]
            if self.options.limit:
                pending = pending[: self.options.limit]
            if not pending:
                log.info("Новых версий нет")
                return result

            authors = read_authors_file(self.work_dir / AUTHORS_FILE_NAME)
            temp_root = self._temp_root()
            window = max(self.options.jobs, 1) + max(self.options.queue_limit, 0)

            futures: dict[int, Future[Path]] = {}
            submitted = 0
            with ThreadPoolExecutor(max_workers=max(self.options.jobs, 1),
                                    thread_name_prefix="gitsync-export") as pool:
                try:
                    for position, version in enumerate(pending):
                        # Пополняем ограниченное окно задач: конвейер не убегает вперёд.
                        while submitted < len(pending) and submitted - position < window:
                            candidate = pending[submitted]
                            futures[candidate.number] = pool.submit(
                                self._export_one, candidate, temp_root, cancel
                            )
                            submitted += 1
                            result.max_inflight = max(result.max_inflight, submitted - position)

                        if cancel.is_set():
                            result.cancelled = True
                            break

                        future = futures.pop(version.number)
                        try:
                            export_dir = future.result()
                        except CancelledError:
                            result.cancelled = True
                            break
                        except BaseException as exc:  # noqa: BLE001 — фиксируем и останавливаем конвейер
                            result.failed_version = version.number
                            result.error = exc
                            log.error("Версия %s не выгружена: %s", version.number, exc)
                            break

                        try:
                            self._commit_version(version, export_dir, authors)
                            result.committed.append(version.number)
                        except BaseException as exc:  # noqa: BLE001
                            result.failed_version = version.number
                            result.error = exc
                            # Возвращаем маркер на последнюю успешную версию до фактического коммита.
                            write_version_file(self.work_dir, current if not result.committed
                                               else result.committed[-1])
                            break
                        finally:
                            if self.options.cleanup_temp:
                                shutil.rmtree(export_dir, ignore_errors=True)

                        if cancel.is_set():
                            result.cancelled = True
                            break
                finally:
                    if result.error is not None or result.cancelled:
                        # Гасим оставшиеся экспортёры, чтобы отмена/сбой укладывались в ограниченное время.
                        cancel.set()
                    for leftover in futures.values():
                        leftover.cancel()

            if self.options.cleanup_temp:
                shutil.rmtree(temp_root, ignore_errors=True)

        if result.error is not None and raise_on_error:
            raise result.error
        return result

    # --- прочие команды ----------------------------------------------------

    def init_working_copy(self, generate_authors: bool = True) -> None:
        """Готовит рабочую копию: git init + служебные файлы AUTHORS/VERSION."""
        self.repo.init()
        version_path = self.work_dir / VERSION_FILE_NAME
        if not version_path.exists():
            write_version_file(self.work_dir, 0)
        authors_path = self.work_dir / AUTHORS_FILE_NAME
        if generate_authors and not authors_path.exists():
            from .authors import write_primary_authors_file
            from .storage_report import authors_from_report

            history = self.backend.fetch_history(1)
            write_primary_authors_file(
                authors_path, authors_from_report(history), self.options.email_domain
            )

    def set_version(self, version: int) -> None:
        write_version_file(self.work_dir, version)

    def needs_sync(self) -> bool:
        current = read_version_file(self.work_dir)
        history = self.backend.fetch_history(current + 1)
        return max((item.number for item in history), default=0) > current
