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
    DesignerError,
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
from .transaction import blob_entry, image, index_entries, locked_index, owned_path, put_image, update_entries
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
RESERVED_INPUT_NAMES = frozenset({".git", "git~1", ".gitsync-py.lock", "authors", "version", ".hooks"})

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
        target = owned_path(work_dir, relative.as_posix())
        put_image(target, image(source))
        written.append(target)
    return written


class _Journal:
    """Журнал незавершённой транзакции версии (durable, в каталоге ``.git``)."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def write(self, payload: dict) -> None:
        import base64

        path = owned_path(self.path.parent, self.path.name)
        put_image(path, {"data": base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode(),
                         "mode": 0o600})

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
        candidate = self.work_dir / "src"
        if version_file_path(self.work_dir).is_file() and version_file_path(candidate).is_file():
            raise UnsafePathError('Ambiguous root and src markers; select explicit target')
        if version_file_path(self.work_dir).is_file():
            return self.work_dir
        if version_file_path(candidate).is_file():
            log.info("Обнаружена раскладка с подкаталогом src: работаю в %s", candidate)
            return candidate
        if candidate.is_dir():
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

    def _validate_transaction(self, entry: dict) -> None:
        if (not isinstance(entry, dict) or entry.get("schema") != 2
                or entry.get("repo") != str(self.repo.path.resolve())
                or entry.get("sync_dir") != str(self.sync_dir.resolve())
                or entry.get("state") not in {"committing", "committed"}
                or not isinstance(entry.get("version"), int)
                or not isinstance(entry.get("files"), dict)
                or not isinstance(entry.get("lock_token"), str)
                or len(entry["lock_token"]) != 32
                or any(c not in "0123456789abcdef" for c in entry["lock_token"])):
            raise UnsafePathError("Invalid or legacy journal; retained for manual recovery")
        prefix = self._rel_to_repo(self.sync_dir)
        directories = entry.get('created_dirs', [])
        if not isinstance(directories, list):
            raise UnsafePathError('Invalid created directories')
        for name in directories:
            owned_path(self.repo.path, name)
            if (name == prefix or (prefix != '.' and not name.startswith(prefix + '/'))
                    or not any(p.startswith(name + '/') and r.get('before') is None
                               and r.get('after') is not None
                               for p, r in entry['files'].items() if isinstance(r, dict))):
                raise UnsafePathError('Unowned created directory')
        for name, record in entry["files"].items():
            owned_path(self.repo.path, name)
            if prefix != "." and not name.startswith(prefix + "/"):
                raise UnsafePathError("Journal target outside selected subtree")
            if (not isinstance(record, dict)
                    or set(record) != {"before", "after", "index_before", "index_after"}):
                raise UnsafePathError("Invalid transaction record")
            for key in ("before", "after"):
                value = record[key]
                if value is not None:
                    import base64
                    if not isinstance(value, dict) or set(value) != {"data", "mode"}:
                        raise UnsafePathError("Invalid preimage")
                    base64.b64decode(value["data"], validate=True)
                    if not isinstance(value["mode"], int) or not 0 <= value["mode"] <= 0o777:
                        raise UnsafePathError("Invalid file mode")
            for key in ("index_before", "index_after"):
                value = record[key]
                if value is not None:
                    import re
                    if (not isinstance(value, str)
                            or not re.fullmatch(r"100(?:644|755) [0-9a-f]{40,64} 0", value)):
                        raise UnsafePathError("Invalid index preimage")

    def _rollback_to_confirmed(self, entry: dict) -> None:
        """Restore recorded preimages only if every affected path is still ours.

        A conflicting editor/index/HEAD stops recovery, retaining the entire journal.
        The mutable index is never treated as a committed baseline.
        """
        self._validate_transaction(entry)
        if self.repo.head_sha() != entry.get("head"):
            raise UnsafePathError("HEAD changed; refusing rollback, journal retained")
        with locked_index(self.repo, entry["lock_token"]) as env:
            current_index = index_entries(self.repo, env)
            for name, record in entry["files"].items():
                current = image(owned_path(self.repo.path, name))
                if current not in (record["before"], record["after"]):
                    raise UnsafePathError(f"External file edit: {name}; journal retained")
                if current_index.get(name) not in (record["index_before"], record["index_after"]):
                    raise UnsafePathError(f"External index edit: {name}; journal retained")
            for name, record in entry["files"].items():
                path = owned_path(self.repo.path, name)
                if image(path) != record["before"]:
                    put_image(path, record["before"])
            update_entries(self.repo, {n: r["index_before"] for n, r in entry["files"].items()}, env)
        self._prune_empty_dirs(entry.get('created_dirs', []))

    def _prune_empty_dirs(self, names: list[str]) -> None:
        """Only recorded candidates, deepest first; rmdir never deletes user contents."""
        for name in sorted(set(names), key=lambda n: len(Path(n).parts), reverse=True):
            folder = owned_path(self.repo.path, name)
            try:
                folder.rmdir()
            except OSError:
                pass  # absent or nonempty: never recurse into a concurrent editor's files

    def _reconcile_journal(self, result: SyncResult) -> None:
        journal = self._journal()
        entry = journal.read()
        if entry is None:
            return
        self._validate_transaction(entry)
        lock = self._git_dir() / 'index.lock'
        if lock.exists():
            if lock.is_symlink() or lock.read_bytes() != ('gitsync:' + entry['lock_token']).encode():
                raise UnsafePathError('Unowned index.lock; journal retained')
            lock.unlink()  # canonical lifecycle lock proves the previous GitSync owner exited
        if entry.get("sha") and self.repo.head_sha() == entry["sha"]:
            with locked_index(self.repo, entry['lock_token']) as env:
                current = index_entries(self.repo, env)
                updates = {}
                for name, record in entry['files'].items():
                    if current.get(name) not in (record['index_before'], record['index_after']):
                        raise UnsafePathError('External index edit after commit; journal retained')
                    updates[name] = record['index_after']
                update_entries(self.repo, updates, env)
            journal.clear()
            return
        self._rollback_to_confirmed(entry)
        result.rolled_back.append(entry["version"])
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
                ctx = self.plugins.emit("before_export", version=version, destination=dest,
                                        cancel=cancel, standard_processing=True)
                if ctx.standard_processing:
                    self.backend.export_version(version.number, dest, cancel)
                self.plugins.emit("after_export", version=version, destination=dest)
                self._verify_export(version, dest)
                validate_export_tree(dest)
                return dest
            except (CancelledError, UnsafePathError, DesignerError):
                # Native errors are not classified transient: never blindly retry
                # invalid authentication, same-login contention or a Designer timeout.
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

    def _target_state(self) -> tuple:
        """Content baseline captured before exporters run; advisory locks do not lock editors."""
        files = {}
        for path in self.sync_dir.rglob('*'):
            rel = path.relative_to(self.sync_dir)
            if rel.parts[0] in {'.git', '.hooks'}:
                continue
            if path.is_file() or path.is_symlink():
                name = path.absolute().relative_to(self.repo.path.absolute()).as_posix()
                files[name] = image(owned_path(self.repo.path, name))
        prefix = self._rel_to_repo(self.sync_dir)
        staged = {n: v for n, v in index_entries(self.repo).items()
                  if prefix == '.' or n.startswith(prefix + '/')}
        return self.repo.head_sha(), files, staged

    def _commit_version(self, version: StorageVersion, export_dir: Path, authors: dict[str, str],
                        previous: int) -> str | None:
        import base64

        if self._target_state() != self._baseline:
            raise UnsafePathError('Target changed during export; no files written')
        cleanup = self.plugins.emit("before_cleanup", version=version, work_dir=self.sync_dir,
                                    standard_processing=True)
        if self._target_state() != self._baseline:
            raise UnsafePathError('Target changed in cleanup hook; no transaction started')
        validate_export_tree(export_dir)
        journal = self._journal()
        before_index = index_entries(self.repo)
        files = {}
        for path in self.sync_dir.rglob("*"):
            relative = path.relative_to(self.sync_dir)
            if relative.parts[0] in SERVICE_NAMES or not cleanup.standard_processing:
                continue
            if path.is_file() or path.is_symlink():
                name = self._rel_to_repo(path)
                files[name] = {"before": image(owned_path(self.repo.path, name)), "after": None}
        for source in export_dir.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(export_dir)
            check_reserved_paths(relative)
            name = (Path(self._rel_to_repo(self.sync_dir)) / relative).as_posix()
            target = owned_path(self.repo.path, name)
            files[name] = {"before": image(target), "after": image(source)}
        marker_name = self._rel_to_repo(version_file_path(self.sync_dir))
        body = '<?xml version="1.0" encoding="UTF-8"?>\n' f'<VERSION>{version.number}</VERSION>\n'
        files[marker_name] = {"before": image(owned_path(self.repo.path, marker_name)),
                              "after": {"data": base64.b64encode(body.encode()).decode(),
                                        "mode": 0o666 if os.name == "nt" else 0o644}}
        for name, record in files.items():
            record["index_before"] = before_index.get(name)
            record["index_after"] = blob_entry(self.repo, record["after"])
        created_dirs, removed_dirs = set(), set()
        for name, record in files.items():
            parent = owned_path(self.repo.path, name).parent
            while parent.absolute() != self.sync_dir.absolute():
                relative = self._rel_to_repo(parent)
                if record['after'] is not None and not parent.exists():
                    created_dirs.add(relative)
                if record['before'] is not None and record['after'] is None:
                    removed_dirs.add(relative)
                parent = parent.parent
        entry = {"schema": 2, "repo": str(self.repo.path.resolve()),
                 "created_dirs": sorted(created_dirs),
                 "sync_dir": str(self.sync_dir.resolve()), "head": self.repo.head_sha(),
                 "version": version.number, "previous": previous, "state": "committing",
                 "files": files, "lock_token": uuid.uuid4().hex}
        self._validate_transaction(entry)
        journal.write(entry)  # every preimage and planned write durable BEFORE mutation
        for name, record in files.items():
            if name == marker_name:
                continue
            target = owned_path(self.repo.path, name)
            if image(target) != record["before"]:
                raise UnsafePathError("External edit before write; journal retained")
            put_image(target, record["after"])
        self._prune_empty_dirs(list(removed_dirs))
        # Keep this explicit seam: a crash before marker is recoverable from the WAL.
        write_version_file(self.sync_dir, version.number)
        signature = author_signature(version.author, authors, self.options.email_domain)
        commit = self.plugins.emit("before_commit", version=version, work_dir=self.sync_dir,
                                   author=signature,
                                   context_values={"message": version.comment, "date": version.date})
        def prepared(sha):
            entry['sha'] = sha
            journal.write(entry)

        sha = self.repo.commit_all(message=commit.message, author=commit.author, date=commit.date,
                                   records=files, expected_head=entry['head'], prepared=prepared,
                                   lock_token=entry['lock_token'])
        entry.update(state='committed', sha=sha)
        journal.write(entry)
        log.info("Версия %s зафиксирована (%s)", version.number, sha or "без изменений")
        return sha

    def _fetch_history(self, start: int, current: int) -> list[StorageVersion]:
        ctx = self.plugins.emit("before_history", start=start, current_version=current,
                                history=None, standard_processing=True)
        history = self.backend.fetch_history(ctx.start) if ctx.standard_processing else ctx.history
        ctx = self.plugins.emit("after_history", history=history, current_version=current)
        if ctx.history is None:
            raise ExportIncompleteError("History override did not provide history")
        history = list(ctx.history)
        numbers = [item.number for item in history]
        if len(numbers) != len(set(numbers)):
            raise ExportIncompleteError("Duplicate history versions are not safe to export")
        return sorted(history, key=lambda item: item.number)

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
            self.plugins.emit("before_sync", work_dir=self.sync_dir)
            if not self.options.allow_dirty:
                self.check_working_copy()

            current = self._read_marker()
            history = self._fetch_history(current + 1, current)
            maximum = max((item.number for item in history), default=0)
            if not history and current > 0:
                # История запрашивается от current+1, поэтому пустой ответ ничего не говорит о
                # реальном максимуме: на живом стенде в журнале это «максимум в хранилище: 0».
                # Порог «хранилище пересоздали» нельзя считать по отфильтрованной истории —
                # берём полный отчёт (лишний вызов только когда новых версий нет).
                maximum = max((item.number for item in self._fetch_history(1, current)), default=0)
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
                self.plugins.emit("after_sync", work_dir=self.sync_dir, result=result)
                return

            self._baseline = self._target_state()
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
                            self.repo.last_commit_ack = None
                            self._commit_version(version, export_dir, authors, previous)
                        except BaseException as exc:  # noqa: BLE001
                            result.failed_version = version.number
                            result.error = exc
                            if self.repo.last_commit_ack:
                                result.committed.append(version.number)
                                result.post_commit = True
                                result.error = PostCommitError(f'Commit durable; bookkeeping failed: {exc}')
                                break
                            entry = self._journal().read()
                            if entry is not None:
                                if entry.get('sha') and self.repo.head_sha() == entry['sha']:
                                    result.committed.append(version.number)
                                    result.post_commit = True
                                    result.error = PostCommitError(
                                        f'Commit durable; bookkeeping failed: {exc}')
                                else:
                                    self._rollback_to_confirmed(entry)
                                    self._journal().clear()
                            break
                        finally:
                            if self.options.cleanup_temp:
                                shutil.rmtree(export_dir, ignore_errors=True)

                        result.committed.append(version.number)
                        self._baseline = self._target_state()
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

            if result.ok:
                self.plugins.emit("after_sync", work_dir=self.sync_dir, result=result)
            if self.options.cleanup_temp:
                # Удаляется только собственный run-каталог: родитель может быть общим.
                shutil.rmtree(temp_root, ignore_errors=True)
                cleanup = getattr(self.backend, "cleanup", None)
                if callable(cleanup):
                    cleanup()

    # --- прочие команды ----------------------------------------------------

    def init_working_copy(self, generate_authors: bool = True, *, raise_on_error: bool = False) -> bool:
        """Returns False on lock contention, or raises when explicitly requested."""
        from .errors import LockBusyError

        try:
            with exclusive_lock(resolve_lock_path(self.work_dir), timeout=self.options.lock_timeout):
                self.sync_dir = self._resolve_sync_dir()
                self.repo.init()
                self._reconcile_journal(SyncResult())
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
            return True
        except LockBusyError:
            if raise_on_error:
                raise
            return False

    def set_version(self, version: int, *, commit: bool = False,
                    author: str = "gitsync <gitsync@localhost>", raise_on_error: bool = False) -> bool:
        from .errors import LockBusyError

        try:
            with exclusive_lock(resolve_lock_path(self.work_dir), timeout=self.options.lock_timeout):
                self.sync_dir = self._resolve_sync_dir()
                if commit and not self.repo.is_repository():
                    raise VersionFileError("set-version --commit requires a Git repository")
                if self.repo.is_repository():
                    self._reconcile_journal(SyncResult())
                path = version_file_path(self.sync_dir)
                name = self._rel_to_repo(path)
                owned_path(self.repo.path, name)
                before = image(path)
                if not commit:
                    # Explicit resume-marker edit is intentionally not a Git transaction.
                    write_version_file(self.sync_dir, version)
                else:
                    self._commit_marker(version, author, name, before)
            return True
        except LockBusyError:
            if raise_on_error:
                raise
            return False

    def _commit_marker(self, version: int, author: str, name: str, before: dict | None):
        """Marker-only transaction using sync's content-bound WAL and reconciliation."""
        import base64

        body = '<?xml version="1.0" encoding="UTF-8"?>\n' f'<VERSION>{version}</VERSION>\n'
        after = {'data': base64.b64encode(body.encode()).decode(),
                 'mode': 0o666 if os.name == 'nt' else 0o644}
        record = {'before': before, 'after': after,
                  'index_before': index_entries(self.repo).get(name),
                  'index_after': blob_entry(self.repo, after)}
        entry = {'schema': 2, 'repo': str(self.repo.path.resolve()), 'created_dirs': [],
                 'sync_dir': str(self.sync_dir.resolve()), 'head': self.repo.head_sha(),
                 'version': version, 'previous': read_version_file(self.sync_dir),
                 'state': 'committing', 'files': {name: record}, 'lock_token': uuid.uuid4().hex}
        self._validate_transaction(entry)
        journal = self._journal()
        wal_started = False

        def apply():
            nonlocal wal_started
            if image(owned_path(self.repo.path, name)) != before:
                raise UnsafePathError('External marker edit before write')
            wal_started = True
            journal.write(entry)
            write_version_file(self.sync_dir, version)

        def prepared(sha):
            entry['sha'] = sha
            journal.write(entry)

        self.repo.last_commit_ack = None
        try:
            sha = self.repo.commit_all(message=f'Установлена версия хранилища {version}',
                                       author=author, records=entry['files'], expected_head=entry['head'],
                                       prepared=prepared, lock_token=entry['lock_token'], apply=apply)
            entry.update(state='committed', sha=sha)
            journal.write(entry)
            journal.clear()
        except Exception as exc:
            if (self.repo.last_commit_ack
                    or (entry.get('sha') and self.repo.head_sha() == entry['sha'])):
                # The ref is the acknowledgement: never roll back a published commit.
                raise PostCommitError(f'Commit durable; bookkeeping failed: {exc}') from exc
            if wal_started:
                self._rollback_to_confirmed(entry)
                journal.clear()
            raise

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
