"""Менеджер синхронизации: параллельная выгрузка версий, последовательные коммиты.

Порядок обработки повторяет upstream (``МенеджерСинхронизации.Синхронизировать``):
прочитать номер синхронизированной версии из ``VERSION``, взять историю хранилища,
для каждой следующей версии — получить исходники, очистить рабочую копию (кроме служебных
файлов), перенести выгрузку, записать ``VERSION`` и закоммитить с автором/датой/комментарием.

Отличие: выгрузка версий идёт параллельно в изолированных каталогах, а коммиты выполняются
строго по возрастанию номера версии в одном потоке. Сбой версии N останавливает конвейер
до версии N — версии N+1.. не коммитятся, ``VERSION`` остаётся на последней успешной версии,
поэтому повторный запуск продолжает ровно с места обрыва и не создаёт дублирующих коммитов.

Транзакционность одной версии обеспечивает журнал в каталоге ``.git`` (см. ``_Journal``):
маркер ``VERSION`` в рабочей копии — намерение, подтверждением считается только коммит в Git.
Незавершённая транзакция откатывается при следующем запуске к последнему подтверждённому
состоянию, причём удаляются лишь файлы, записанные этим же инструментом.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from .authors import author_signature, read_authors_file
from .errors import (
    CancelledError,
    DirtyWorkingCopyError,
    ExportIncompleteError,
    PostCommitError,
    StorageVersionMismatchError,
    UnsafePathError,
    VersionFileError,
)
from .gitrepo import GitRepo
from .locks import exclusive_lock
from .plugins import PluginHost
from .safepath import safe_join
from .storage_report import StorageVersion
from .version_file import (
    AUTHORS_FILE_NAME,
    VERSION_FILE_NAME,
    read_version_file,
    read_version_file_strict,
    version_file_path,
    write_version_file,
)

log = logging.getLogger("gitsync.sync")

#: Файлы и каталоги рабочей копии, которые не удаляются при очистке (как в upstream).
SERVICE_NAMES = frozenset(
    {".git", ".gitignore", ".gitattributes", ".hooks", AUTHORS_FILE_NAME, VERSION_FILE_NAME}
)

#: Имена, которые нельзя брать из внешней выгрузки ни на каком уровне вложенности.
#: ``.git`` — управляющий каталог Git: подмена ``config``/``hooks`` меняет поведение Git.
#: ``git~1`` — короткое имя NTFS для ``.git``; Windows отбрасывает хвостовые точки и пробелы,
#: поэтому сравнение идёт по нормализованному имени без регистра.
RESERVED_INPUT_NAMES = frozenset({".git", "git~1", ".gitsync-py.lock"})

#: Имя файла журнала транзакции внутри каталога ``.git``.
JOURNAL_FILE_NAME = "gitsync-py-journal.json"

#: Имя файла блокировки внутри каталога ``.git``.
LOCK_FILE_NAME = "gitsync-py.lock"

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
    disable_auto_src: bool = False


@dataclass
class SyncResult:
    committed: list[int] = field(default_factory=list)
    failed_version: int | None = None
    error: BaseException | None = None
    cancelled: bool = False
    max_inflight: int = 0
    #: Ошибка возникла ПОСЛЕ успешного коммита (обработчик after_commit): коммит и маркер целы.
    post_commit: bool = False
    #: Версии, снятые при старте как незавершённые транзакции предыдущего запуска.
    rolled_back: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.cancelled


def is_reserved_input_name(name: str) -> bool:
    """Зарезервированное служебное имя во входящей выгрузке (регистр и хвосты Windows)."""
    # Поток данных NTFS: "имя:поток" обращается к тому же файлу "имя".
    base = name.split(":", 1)[0]
    normalized = base.rstrip(" .").casefold()
    return normalized in RESERVED_INPUT_NAMES


def check_reserved_paths(relative: PurePath) -> None:
    for part in relative.parts:
        if is_reserved_input_name(part):
            raise UnsafePathError(
                f"Выгрузка пытается записать служебный путь Git <{relative}>; "
                "такие имена не принимаются из внешних данных"
            )


def validate_export_tree(root: Path) -> None:
    """Проверяет, что выгрузка не выходит за свой каталог, без симлинков и служебных имён."""
    root_resolved = root.resolve()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise UnsafePathError(f"Выгрузка содержит символьную ссылку, это небезопасно: {item}")
        check_reserved_paths(PurePath(item.relative_to(root)))
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


def move_export_into_working_copy(work_dir: Path, export_dir: Path) -> list[Path]:
    """Переносит выгрузку в рабочую копию. Возвращает список записанных файлов."""
    written: list[Path] = []
    for source in sorted(export_dir.rglob("*")):
        relative = PurePath(source.relative_to(export_dir))
        check_reserved_paths(relative)
        target = safe_join(work_dir, relative)
        if source.is_dir() and not source.is_symlink():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            # Иначе copyfile пишет ПО ссылке — наружу рабочей копии.
            target.unlink()
        shutil.copyfile(source, target)
        written.append(target)
    return written


class _Journal:
    """Журнал незавершённой транзакции версии (durable, в каталоге ``.git``)."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        # Журнал должен пережить отключение процесса, иначе откат нечем обосновать.
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class SyncManager:
    """Оркестратор синхронизации одного хранилища с одной рабочей копией."""

    def __init__(self, work_dir: str | Path, backend, options: SyncOptions | None = None,
                 plugins: PluginHost | None = None):
        self.work_dir = Path(work_dir)
        self.backend = backend
        self.options = options or SyncOptions()
        # Репозиторий — НАСТОЯЩИЙ корень Git: вложенный .git внутри существующего репозитория
        # делает всё дерево untracked и приводит к «грязной копии» на ровном месте.
        self.repo = GitRepo(discover_repo_root(self.work_dir))
        self.plugins = plugins or PluginHost()
        self.sync_dir = self.work_dir

    # --- размещение ------------------------------------------------------

    def _resolve_sync_dir(self) -> Path:
        """Каталог выгрузки: сам workdir или подкаталог ``src`` (раскладка upstream)."""
        if self.options.disable_auto_src:
            return self.work_dir
        if version_file_path(self.work_dir).is_file():
            return self.work_dir
        candidate = self.work_dir / "src"
        if version_file_path(candidate).is_file():
            log.info("Обнаружена раскладка с подкаталогом src: работаю в %s", candidate)
            return candidate
        return self.work_dir

    def _git_dir(self) -> Path:
        result = self.repo.run(["rev-parse", "--absolute-git-dir"], check=False)
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
        return self.repo.path / ".git"

    def _rel_to_repo(self, path: Path) -> str:
        try:
            relative = path.resolve().relative_to(self.repo.path.resolve())
        except ValueError:
            return "."
        return relative.as_posix() or "."

    def _journal(self) -> _Journal:
        return _Journal(self._git_dir() / JOURNAL_FILE_NAME)

    # --- состояние рабочей копии ------------------------------------------

    def check_working_copy(self) -> None:
        """Грязная рабочая копия защищается; служебные файлы gitsync внутри цели — исключение."""
        status = self.repo.run(["status", "--porcelain", "--untracked-files=all"]).stdout
        prefix = self._rel_to_repo(self.sync_dir)
        prefix = "" if prefix == "." else prefix + "/"
        dirty: list[str] = []
        for line in status.splitlines():
            entry = line[3:].strip().strip('"')
            if not entry:
                continue
            if entry.startswith(prefix):
                top = entry[len(prefix):].split("/")[0]
                if top in SERVICE_NAMES:
                    continue
            dirty.append(entry)
        if dirty:
            raise DirtyWorkingCopyError(
                f"В рабочей копии <{self.repo.path}> есть чужие незафиксированные изменения: "
                + ", ".join(dirty[:20])
                + "\nЗафиксируйте или уберите их (или используйте --allow-dirty, если это ваши артефакты)."
            )

    def _committed_marker(self) -> int | None:
        """Номер версии из ПОДТВЕРЖДЁННОГО коммита (``HEAD``), либо None если его нет."""
        rel = self._rel_to_repo(self.sync_dir)
        path = VERSION_FILE_NAME if rel == "." else f"{rel}/{VERSION_FILE_NAME}"
        result = self.repo.run(["show", f"HEAD:{path}"], check=False)
        if result.returncode != 0:
            return None
        match = read_version_from_text(result.stdout)
        return match

    def _read_marker(self) -> int:
        """Маркер рабочей копии. Отсутствие маркера в непустом репозитории — авария (fail closed)."""
        path = version_file_path(self.sync_dir)
        if path.is_file():
            return read_version_file_strict(self.sync_dir)
        if self.repo.commit_count() > 0:
            raise VersionFileError(
                f"Файл <{path}> отсутствует, хотя репозиторий уже содержит коммиты. "
                "Молча начать с нуля нельзя — это повторит всю историю поверх существующей. "
                "Восстановите файл (git checkout), либо задайте номер командой set-version, "
                "либо подготовьте новую копию командой init."
            )
        return 0

    # --- транзакция --------------------------------------------------------

    def _tracked_files(self) -> set[str]:
        rel = self._rel_to_repo(self.sync_dir)
        args = ["ls-files", "--"] + ([rel] if rel != "." else ["."])
        result = self.repo.run(args, check=False)
        return {line.strip().strip('"') for line in result.stdout.splitlines() if line.strip()}

    def _rollback_to_confirmed(self, written: list[str]) -> None:
        """Возвращает СВОИ записи к последнему подтверждённому коммиту.

        Чужие файлы не трогаются: удаляются только пути из журнала, которых нет в индексе.
        """
        has_head = self.repo.head_sha() is not None
        tracked = self._tracked_files() if has_head else set()
        for entry in written:
            if entry in tracked:
                continue
            candidate = self.repo.path / entry
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink(missing_ok=True)
        if has_head:
            rel = self._rel_to_repo(self.sync_dir)
            target = "." if rel == "." else rel
            # Снимаем возможную индексацию и возвращаем содержимое своей подпапки из HEAD.
            self.repo.run(["reset", "--quiet", "--", target], check=False)
            self.repo.run(["checkout", "--force", "HEAD", "--", target], check=False)

    def _reconcile_journal(self, result: SyncResult) -> None:
        """Сверяет журнал с подтверждённым коммитом и снимает незавершённую транзакцию."""
        journal = self._journal()
        entry = journal.read()
        if not entry:
            return
        version = entry.get("version")
        confirmed = self._committed_marker()
        marker_path = version_file_path(self.sync_dir)
        marker = read_version_file(self.sync_dir) if marker_path.is_file() else None
        if entry.get("state") == "committed":
            journal.clear()
            return
        if marker == version and (confirmed is None or confirmed < version):
            log.warning(
                "Найдена незавершённая транзакция версии %s (в Git подтверждена %s) — "
                "откатываю свои записи и повторяю версию", version, confirmed
            )
            self._rollback_to_confirmed(list(entry.get("written", [])))
            if confirmed is None:
                write_version_file(self.sync_dir, entry.get("previous", 0))
            if isinstance(version, int):
                result.rolled_back.append(version)
        journal.clear()

    # --- вспомогательное -------------------------------------------------

    def _temp_root(self) -> Path:
        """Собственный каталог запуска внутри переданного родителя.

        Родительский каталог может быть общим (несколько репозиториев, параллельные запуски),
        поэтому удаляется ТОЛЬКО созданный здесь ``run-<uuid>``, а не сам родитель.
        """
        # По умолчанию — рядом с корнем репозитория, а не рядом с подкаталогом выгрузки:
        # иначе временный каталог оказался бы внутри рабочей копии и попал под очистку.
        parent = Path(self.options.temp_root or (self.repo.path.parent / ".gitsync-tmp"))
        parent_resolved = parent.resolve() if parent.exists() else parent.absolute()
        repo_resolved = self.repo.path.resolve() if self.repo.path.exists() else self.repo.path.absolute()
        if parent_resolved == repo_resolved or repo_resolved in parent_resolved.parents:
            raise UnsafePathError(
                f"Каталог временных файлов <{parent}> находится внутри рабочей копии "
                f"<{self.repo.path}>: очистка рабочей копии уничтожила бы выгрузку"
            )
        parent.mkdir(parents=True, exist_ok=True)
        run_root = parent / f"run-{uuid.uuid4().hex}"
        run_root.mkdir(exist_ok=False)
        return run_root

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
                self._verify_export(version, dest)
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

    def _verify_export(self, version: StorageVersion, dest: Path) -> None:
        """Выгрузка считается состоявшейся только по факту артефакта, а не по отсутствию ошибки.

        Отсутствующий каталог — всегда авария: иначе «ничего не выгрузилось» превращается в
        успешный пустой snapshot и коммит удаления всей конфигурации. Пустой каталог тоже
        отвергается, если бэкенд явно не объявил ``allows_empty_export`` — upstream коммитит
        пустые версии, поэтому запрет сделан отключаемым, а не абсолютным.
        """
        if not dest.is_dir():
            raise ExportIncompleteError(
                f"Бэкенд не создал каталог выгрузки версии {version.number}: {dest}. "
                "Выгрузка не подтверждена, синхронизация остановлена."
            )
        if any(dest.iterdir()):
            return
        if getattr(self.backend, "allows_empty_export", False):
            log.warning("Версия %s: выгрузка пуста, бэкенд считает это допустимым", version.number)
            return
        raise ExportIncompleteError(
            f"Выгрузка версии {version.number} пуста: {dest}. Если пустая версия легитимна, "
            "бэкенд должен объявить allows_empty_export = True."
        )

    def _commit_version(self, version: StorageVersion, export_dir: Path, authors: dict[str, str],
                        previous: int) -> str | None:
        journal = self._journal()
        journal.write({"version": version.number, "state": "committing", "previous": previous,
                       "sync_dir": str(self.sync_dir), "written": []})
        clean_working_copy(self.sync_dir)
        written = move_export_into_working_copy(self.sync_dir, export_dir)
        marker = write_version_file(self.sync_dir, version.number)
        journal.write({
            "version": version.number,
            "state": "committing",
            "previous": previous,
            "sync_dir": str(self.sync_dir),
            "written": [self._rel_to_repo(item) for item in [*written, marker]],
        })
        signature = author_signature(version.author, authors, self.options.email_domain)
        self.plugins.emit("before_commit", version=version, work_dir=self.sync_dir, author=signature)
        sha = self.repo.commit_all(message=version.comment, author=signature, date=version.date)
        journal.write({"version": version.number, "state": "committed", "sha": sha or ""})
        log.info("Версия %s зафиксирована (%s)", version.number, sha or "без изменений")
        return sha

    # --- основной сценарий ------------------------------------------------

    def sync(self, cancel: threading.Event | None = None, raise_on_error: bool = True) -> SyncResult:
        cancel = cancel or threading.Event()
        result = SyncResult()
        try:
            self._run_sync(cancel, result)
        except Exception as exc:  # noqa: BLE001 — контракт raise_on_error един для всех стадий
            # Отказы подготовки (маркер, блокировка, грязная копия, состояние хранилища)
            # возвращаются так же, как отказы конвейера: вызывающий сам решает, падать ли.
            if result.error is None:
                result.error = exc
        if result.error is not None and raise_on_error:
            raise result.error
        return result

    def _run_sync(self, cancel: threading.Event, result: SyncResult) -> None:
        self.repo.init()
        self.sync_dir = self._resolve_sync_dir()
        self.sync_dir.mkdir(parents=True, exist_ok=True)

        lock_path = self._git_dir() / LOCK_FILE_NAME
        with exclusive_lock(lock_path, timeout=self.options.lock_timeout):
            # Незавершённая транзакция прошлого запуска снимается ДО проверки грязной копии:
            # иначе собственные недописанные файлы выглядят как чужие правки.
            self._reconcile_journal(result)
            if not self.options.allow_dirty:
                self.check_working_copy()

            current = self._read_marker()
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
                return

            authors = read_authors_file(self.sync_dir / AUTHORS_FILE_NAME)
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

                        previous = result.committed[-1] if result.committed else current
                        try:
                            self._commit_version(version, export_dir, authors, previous)
                        except BaseException as exc:  # noqa: BLE001
                            result.failed_version = version.number
                            result.error = exc
                            # Коммита не было: снимаем собственные записи до подтверждённого
                            # состояния, чтобы следующий запуск не считал их чужой грязью.
                            entry = self._journal().read() or {}
                            self._rollback_to_confirmed(list(entry.get("written", [])))
                            self._journal().clear()
                            write_version_file(self.sync_dir, previous)
                            break
                        finally:
                            if self.options.cleanup_temp:
                                shutil.rmtree(export_dir, ignore_errors=True)

                        result.committed.append(version.number)
                        try:
                            # Коммит уже зафиксирован: ошибка обработчика не отменяет его.
                            self.plugins.emit("after_commit", version=version,
                                              work_dir=self.sync_dir, sha=self.repo.head_sha())
                        except BaseException as exc:  # noqa: BLE001
                            result.failed_version = version.number
                            result.post_commit = True
                            result.error = PostCommitError(
                                f"Версия {version.number} зафиксирована в Git, но обработчик "
                                f"after_commit завершился с ошибкой: {exc}"
                            )
                            result.error.__cause__ = exc
                            self._journal().clear()
                            break
                        self._journal().clear()

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
                # Удаляется только собственный run-каталог: родитель может быть общим.
                shutil.rmtree(temp_root, ignore_errors=True)
                cleanup = getattr(self.backend, "cleanup", None)
                if callable(cleanup):
                    cleanup()

    # --- прочие команды ----------------------------------------------------

    def init_working_copy(self, generate_authors: bool = True) -> None:
        """Готовит рабочую копию: git init + служебные файлы AUTHORS/VERSION."""
        self.repo.init()
        self.sync_dir = self._resolve_sync_dir()
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        version_path = version_file_path(self.sync_dir)
        if not version_path.exists():
            write_version_file(self.sync_dir, 0)
        authors_path = self.sync_dir / AUTHORS_FILE_NAME
        if generate_authors and not authors_path.exists():
            from .authors import write_primary_authors_file
            from .storage_report import authors_from_report

            history = self.backend.fetch_history(1)
            write_primary_authors_file(
                authors_path, authors_from_report(history), self.options.email_domain
            )

    def set_version(self, version: int) -> None:
        self.sync_dir = self._resolve_sync_dir()
        write_version_file(self.sync_dir, version)

    def needs_sync(self) -> bool:
        self.sync_dir = self._resolve_sync_dir()
        current = read_version_file(self.sync_dir)
        history = self.backend.fetch_history(current + 1)
        return max((item.number for item in history), default=0) > current


def discover_repo_root(work_dir: str | Path) -> Path:
    """Настоящий корень Git для каталога или он сам, если репозитория ещё нет."""
    path = Path(work_dir)
    if not path.is_dir():
        return path
    result = GitRepo(path).run(["rev-parse", "--show-toplevel"], check=False)
    if result.returncode == 0 and result.stdout.strip():
        root = Path(result.stdout.strip())
        if root.exists():
            return root
    return path


def resolve_lock_path(work_dir: str | Path) -> Path:
    """Канонический файл блокировки: он один на настоящий индекс Git, а не на подкаталог."""
    root = discover_repo_root(work_dir)
    repo = GitRepo(root)
    result = repo.run(["rev-parse", "--absolute-git-dir"], check=False) if root.is_dir() else None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()) / LOCK_FILE_NAME
    return root / ".git" / LOCK_FILE_NAME


def read_version_from_text(raw: str) -> int | None:
    from .version_file import _VERSION_RE

    match = _VERSION_RE.search(raw or "")
    text = (match.group(1) if match else (raw or "").strip())
    digits = "".join(ch for ch in text if not ch.isspace())
    try:
        return int(digits)
    except ValueError:
        return None


def check_working_copy(repo: GitRepo) -> None:
    """Совместимость: проверка грязной копии для каталога == корня репозитория."""
    manager_status = repo.run(["status", "--porcelain", "--untracked-files=all"]).stdout
    dirty: list[str] = []
    for line in manager_status.splitlines():
        entry = line[3:].strip().strip('"')
        if not entry:
            continue
        if entry.split("/")[0] in SERVICE_NAMES:
            continue
        dirty.append(entry)
    if dirty:
        raise DirtyWorkingCopyError(
            f"В рабочей копии <{repo.path}> есть чужие незафиксированные изменения: "
            + ", ".join(dirty[:20])
            + "\nЗафиксируйте или уберите их (или используйте --allow-dirty, если это ваши артефакты)."
        )
