"""Слайс 2: интеграция с настоящим git (subprocess), блокировки и защита грязной копии."""

from __future__ import annotations

import datetime as dt
import subprocess
import threading
import time

import pytest

from gitsync.errors import DirtyWorkingCopyError, LockBusyError
from gitsync.gitrepo import GitRepo
from gitsync.locks import exclusive_lock


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


@pytest.fixture()
def repo(tmp_path):
    work = tmp_path / "рабочая копия с пробелами"
    work.mkdir()
    r = GitRepo(work)
    r.init()
    return r


def test_init_creates_real_repository(repo):
    assert (repo.path / ".git").is_dir()
    assert repo.is_repository()


def test_commit_preserves_author_date_and_multiline_comment(repo):
    (repo.path / "файл с пробелом.txt").write_text("данные", encoding="utf-8")
    when = dt.datetime(2026, 9, 15, 10, 20, 30)

    sha = repo.commit_all(
        message="Версия 1\n\nмногострочный комментарий",
        author="Иванов <ivanov@example.org>",
        date=when,
    )

    assert sha
    out = _git(repo.path, "log", "-1", "--format=%an|%ae|%ad|%B", "--date=format:%Y-%m-%d %H:%M:%S")
    name, email, date, body = out.split("|", 3)
    assert name == "Иванов"
    assert email == "ivanov@example.org"
    assert date == "2026-09-15 10:20:30"
    assert body.strip() == "Версия 1\n\nмногострочный комментарий"
    assert _git(repo.path, "log", "-1", "--format=%cn|%cd", "--date=format:%Y-%m-%d %H:%M:%S").strip() == (
        "Иванов|2026-09-15 10:20:30"
    )


def test_commit_handles_empty_comment_like_upstream(repo):
    (repo.path / "a.txt").write_text("1", encoding="utf-8")
    repo.commit_all(message="", author="И <i@e.org>", date=dt.datetime(2026, 1, 1))
    assert _git(repo.path, "log", "-1", "--format=%B").strip() == "."


def test_commit_records_deletions_and_cyrillic_paths(repo):
    nested = repo.path / "каталог" / "подкаталог"
    nested.mkdir(parents=True)
    (nested / "объект.xml").write_text("v1", encoding="utf-8")
    repo.commit_all(message="v1", author="И <i@e.org>", date=dt.datetime(2026, 1, 1))

    (nested / "объект.xml").unlink()
    (repo.path / "новый.xml").write_text("v2", encoding="utf-8")
    repo.commit_all(message="v2", author="И <i@e.org>", date=dt.datetime(2026, 1, 2))

    tracked = _git(repo.path, "ls-files").splitlines()
    assert "новый.xml" in tracked
    assert not any("объект.xml" in item for item in tracked)


def test_commit_without_changes_returns_none_and_does_not_duplicate(repo):
    (repo.path / "a.txt").write_text("1", encoding="utf-8")
    repo.commit_all(message="v1", author="И <i@e.org>", date=dt.datetime(2026, 1, 1))
    before = repo.commit_count()

    assert repo.commit_all(message="v1", author="И <i@e.org>", date=dt.datetime(2026, 1, 1)) is None
    assert repo.commit_count() == before


def test_dirty_working_copy_is_detected(repo):
    (repo.path / "a.txt").write_text("1", encoding="utf-8")
    repo.commit_all(message="v1", author="И <i@e.org>", date=dt.datetime(2026, 1, 1))
    assert repo.is_clean()

    (repo.path / "a.txt").write_text("изменено вручную", encoding="utf-8")
    assert not repo.is_clean()
    with pytest.raises(DirtyWorkingCopyError):
        repo.ensure_clean()


def test_sync_never_pushes_implicitly(repo):
    """Апстрим не делает push; фиксируем это как контракт: в git не уходит ни одна сетевая команда."""
    calls: list[list[str]] = []
    original = repo.run

    def spy(args, **kwargs):
        calls.append(list(args))
        return original(args, **kwargs)

    repo.run = spy  # type: ignore[method-assign]
    (repo.path / "a.txt").write_text("1", encoding="utf-8")
    repo.commit_all(message="v1", author="И <i@e.org>", date=dt.datetime(2026, 1, 1))

    forbidden = {"push", "remote", "fetch", "pull"}
    assert not [call for call in calls if forbidden & set(call)]


def test_exclusive_lock_blocks_second_holder(tmp_path):
    lock_path = tmp_path / "цель.lock"
    with exclusive_lock(lock_path, timeout=0.1):
        with pytest.raises(LockBusyError):
            with exclusive_lock(lock_path, timeout=0.2):
                pass


def test_exclusive_lock_is_released_on_error_and_reusable(tmp_path):
    lock_path = tmp_path / "цель.lock"
    with pytest.raises(RuntimeError):
        with exclusive_lock(lock_path, timeout=0.1):
            raise RuntimeError("сбой внутри критической секции")
    with exclusive_lock(lock_path, timeout=0.1):
        pass


def test_exclusive_lock_serializes_threads(tmp_path):
    lock_path = tmp_path / "цель.lock"
    order: list[str] = []
    started = threading.Event()

    def worker():
        with exclusive_lock(lock_path, timeout=5.0):
            order.append("второй")

    with exclusive_lock(lock_path, timeout=5.0):
        thread = threading.Thread(target=worker)
        thread.start()
        started.set()
        time.sleep(0.15)
        order.append("первый")
    thread.join(timeout=5)
    assert order == ["первый", "второй"]
