"""Работа с настоящим git через subprocess (argv-массивы, без shell).

Апстрим коммитит через gitrunner: ``git add -A .`` + ``git commit`` с явными author/committer
и датой. Здесь то же самое, но дата и автор передаются через переменные окружения
``GIT_AUTHOR_*``/``GIT_COMMITTER_*``, чтобы не зависеть от разбора локали.
Push и любые сетевые операции не выполняются — это осознанный контракт (как в upstream).
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
    ) -> str | None:
        """``git add -A .`` + commit. Возвращает SHA или None, если коммитить нечего."""
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

    def last_commit_message(self) -> str:
        return self.run(["log", "-1", "--format=%B"], check=False).stdout.strip()
