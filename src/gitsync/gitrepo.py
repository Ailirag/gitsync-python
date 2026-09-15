"""Работа с настоящим git через subprocess (argv-массивы, без shell).

Апстрим коммитит через gitrunner: ``git add -A .`` + ``git commit`` с явными author/committer
и датой. Здесь то же самое, но дата и автор передаются через переменные окружения
``GIT_AUTHOR_*``/``GIT_COMMITTER_*``, чтобы не зависеть от разбора локали.
Обычный sync не выполняет сетевых операций; clone — явно запрошенная операция Git.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import subprocess
from pathlib import Path

from .errors import DirtyWorkingCopyError, GitSyncError

log = logging.getLogger("gitsync.git")

_SIGNATURE_RE = re.compile(r"^\s*(?P<name>.*?)\s*<(?P<email>[^>]*)>\s*$")
DEFAULT_GIT_TIMEOUT = 600.0


def split_signature(signature: str) -> tuple[str, str]:
    """``Иванов <i@e.org>`` -> ``("Иванов", "i@e.org")``."""
    match = _SIGNATURE_RE.match(signature or "")
    if match:
        return match.group("name") or "gitsync", match.group("email")
    name = (signature or "gitsync").strip()
    return name, ""


def format_git_date(value: dt.datetime) -> str:
    """Дата для git в ISO-подобном виде с явным смещением (naive считается локальной)."""
    if value.tzinfo is None:
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return value.strftime("%Y-%m-%dT%H:%M:%S%z")


class GitRepo:
    """Тонкая обёртка над git-процессом для одной рабочей копии."""

    def __init__(self, path: str | Path, git_path: str = "git", timeout: float = DEFAULT_GIT_TIMEOUT):
        self.path = Path(path)
        self.git_path = git_path
        self.timeout = timeout

    # --- низкий уровень -------------------------------------------------

    def run(self, args: list[str], check: bool = True, env: dict[str, str] | None = None,
            timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        argv = [self.git_path, *args]
        process_env = dict(os.environ)
        # Гарантируем машинно-читаемый вывод независимо от локали пользователя.
        process_env.setdefault("LC_ALL", "C")
        if env:
            process_env.update(env)
        log.debug("git %s (cwd=%s)", " ".join(args), self.path)
        result = subprocess.run(
            argv,
            cwd=str(self.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=process_env,
            timeout=timeout or self.timeout,
            shell=False,
            check=False,
        )
        if check and result.returncode != 0:
            raise GitSyncError(
                f"git {' '.join(args)} завершился с кодом {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return result

    # --- состояние ------------------------------------------------------

    def is_repository(self) -> bool:
        if not (self.path / ".git").exists():
            return False
        return self.run(["rev-parse", "--is-inside-work-tree"], check=False).returncode == 0

    def init(self, initial_branch: str = "main") -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        if self.is_repository():
            return
        self.run(["init", "-b", initial_branch])
        # Выгрузка 1С содержит кириллицу в именах — не даём git ломать их в выводе.
        self.run(["config", "core.quotepath", "false"])
        # Выгрузка конфигуратора должна лежать в git побайтово. На машине с глобальным
        # core.autocrlf=true (проверено на стенде) блоб терял по байту на строку, и дерево
        # коммита переставало совпадать с XML платформы. Настройка локальная — чужие
        # репозитории и глобальный конфиг не трогаем.
        self.run(["config", "core.autocrlf", "false"])

    def clone(self, url: str) -> None:
        """Explicit Git clone; failures do not disclose the remote URL or credentials."""
        if self.path.is_symlink() or (self.path.exists() and (
                not self.path.is_dir() or any(self.path.iterdir()))):
            raise GitSyncError("Refusing nonempty destination (or link/file)")
        from urllib.parse import urlsplit
        from urllib.request import url2pathname

        parsed = urlsplit(url)
        if parsed.scheme == 'file' and parsed.netloc in ('', 'localhost'):
            url = url2pathname(parsed.path)
        elif Path(url).exists():
            url = str(Path(url).absolute())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [self.git_path, "-c", "core.autocrlf=false", "clone", "--", url, str(self.path.absolute())],
            cwd=self.path.parent, capture_output=True, timeout=self.timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=False,
        )
        if result.returncode:
            raise GitSyncError(f"git clone failed (exit {result.returncode}); destination retained")
        self.run(["config", "core.autocrlf", "false"])
        self.run(["config", "core.quotepath", "false"])

    def is_clean(self) -> bool:
        result = self.run(["status", "--porcelain", "--untracked-files=all"])
        return not result.stdout.strip()

    def ensure_clean(self) -> None:
        if not self.is_clean():
            status = self.run(["status", "--porcelain", "--untracked-files=all"]).stdout.strip()
            raise DirtyWorkingCopyError(
                f"В рабочей копии <{self.path}> есть незафиксированные изменения:\n{status}\n"
                "Зафиксируйте или уберите их — gitsync-py не трогает чужие правки."
            )

    def commit_count(self) -> int:
        result = self.run(["rev-list", "--count", "HEAD"], check=False)
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip() or 0)

    def head_sha(self) -> str | None:
        result = self.run(["rev-parse", "HEAD"], check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def has_staged_or_unstaged_changes(self) -> bool:
        return not self.is_clean()

    # --- коммит ---------------------------------------------------------

    def commit_all(
        self,
        message: str,
        author: str,
        date: dt.datetime | None = None,
        committer: str | None = None,
        *,
        records: dict | None = None,
        expected_head: str | None = None,
        prepared=None,
        lock_token: str | None = None,
        apply=None,
    ) -> str | None:
        """Legacy explicit all-files commit, or a content-bound isolated transaction."""
        if records is not None:
            return self._commit_scoped(message, author, date, committer, records,
                                       expected_head, prepared, lock_token, apply)
        self.run(["add", "-A", "."])
        if self.is_clean():
            log.debug("Нет изменений — коммит пропущен")
            return None

        when = date or dt.datetime.now()
        author_name, author_email = split_signature(author)
        committer_name, committer_email = split_signature(committer or author)
        stamp = format_git_date(when)
        env = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": committer_name,
            "GIT_COMMITTER_EMAIL": committer_email,
            "GIT_COMMITTER_DATE": stamp,
        }
        # Пустой комментарий апстрим заменяет точкой — сохраняем поведение.
        text = message if message and message.strip() else "."
        # Комментарий передаётся через stdin: в нём бывают переводы строк и кириллица.
        result = subprocess.run(
            [self.git_path, "commit", "--no-verify", "--cleanup=verbatim", "-F", "-"],
            cwd=str(self.path),
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "LC_ALL": "C", **env},
            timeout=self.timeout,
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            raise GitSyncError(
                f"git commit завершился с кодом {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return self.head_sha()

    def _commit_scoped(self, message, author, date, committer, records, head, prepared, lock_token,
                       apply):
        import tempfile

        from .transaction import image, index_entries, locked_index, owned_path, update_entries

        self.last_commit_ack = None
        stamp = format_git_date(date or dt.datetime.now())
        an, ae = split_signature(author)
        cn, ce = split_signature(committer or author)
        identity = {'GIT_AUTHOR_NAME': an, 'GIT_AUTHOR_EMAIL': ae, 'GIT_AUTHOR_DATE': stamp,
                    'GIT_COMMITTER_NAME': cn, 'GIT_COMMITTER_EMAIL': ce, 'GIT_COMMITTER_DATE': stamp}
        with locked_index(self, lock_token) as user_env:
            if self.head_sha() != head:
                raise GitSyncError('HEAD changed before publication')
            current = index_entries(self, user_env)
            for name, record in records.items():
                if current.get(name) != record['index_before']:
                    raise GitSyncError(f'External staged edit: {name}')
            if apply:
                apply()  # WAL and marker mutation only after acquiring the real Git index lock.
            for name, record in records.items():
                if image(owned_path(self.path, name)) != record['after']:
                    raise GitSyncError(f'External worktree edit: {name}')
            # Tree starts from HEAD, not from the user's staged changes.
            gitdir = Path(user_env['GIT_INDEX_FILE']).parent
            fd, name = tempfile.mkstemp(prefix='gitsync-tree-', dir=gitdir)
            os.close(fd)
            isolated = Path(name)
            isolated.unlink()
            try:
                env = {**identity, 'GIT_INDEX_FILE': str(isolated)}
                self.run(['read-tree', head] if head else ['read-tree', '--empty'], env=env)
                updates = {n: r['index_after'] for n, r in records.items()}
                update_entries(self, updates, env)
                tree = self.run(['write-tree'], env=env).stdout.strip()
                args = ['commit-tree', tree, *(['-p', head] if head else []),
                        '-m', message if message and message.strip() else '.']
                sha = self.run(args, env=env).stdout.strip()
                if prepared:
                    prepared(sha)  # WAL knows exact commit identity before ref publication
                # Stage only our paths in a private copy of the current user index.
                update_entries(self, updates, user_env)
                self.run(['update-ref', 'HEAD', sha, head or '0' * 40])
                self.last_commit_ack = sha  # durable Git acknowledgement, before bookkeeping
                return sha
            finally:
                isolated.unlink(missing_ok=True)

    def last_commit_message(self) -> str:
        return self.run(["log", "-1", "--format=%B"], check=False).stdout.strip()
