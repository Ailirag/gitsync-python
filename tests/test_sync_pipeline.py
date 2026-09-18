"""Слайс 3: параллельная выгрузка версий при строго последовательных коммитах.

Ключевые свойства, которые здесь проверяются на настоящем git:
* коммиты идут строго по возрастанию номера версии, даже если экспорт завершается вразнобой;
* сохраняются автор, дата, комментарий и номер версии (файл VERSION);
* сбой версии не оставляет «дырок» и не коммитит следующие версии;
* повторный запуск (resume) не создаёт дублирующих коммитов;
* отмена укладывается в ограниченное время;
* конвейер ограничен по числу одновременно работающих экспортёров.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import threading
import time

import pytest

from gitsync.backends import FakeStorageBackend
from gitsync.errors import DirtyWorkingCopyError, StorageVersionMismatchError, UnsafePathError
from gitsync.gitrepo import GitRepo
from gitsync.storage_report import StorageVersion
from gitsync.sync import SyncManager, SyncOptions
from gitsync.version_file import read_version_file


def _versions(count: int = 5) -> list[StorageVersion]:
    return [
        StorageVersion(
            number=i,
            author=f"Автор{i % 2}",
            date=dt.datetime(2026, 9, 10, 12, 0, 0) + dt.timedelta(days=i),
            comment=f"Версия {i}",
        )
        for i in range(1, count + 1)
    ]


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


@pytest.fixture()
def work_dir(tmp_path):
    path = tmp_path / "рабочая копия"
    path.mkdir()
    GitRepo(path).init()
    return path


def test_sync_commits_in_version_order_despite_out_of_order_export(work_dir, tmp_path):
    # Ранние версии экспортируются медленнее поздних — порядок завершения обратный.
    backend = FakeStorageBackend(_versions(5), delays={1: 0.30, 2: 0.24, 3: 0.16, 4: 0.08, 5: 0.0})
    manager = SyncManager(work_dir, backend, SyncOptions(jobs=5, temp_root=tmp_path / "tmp"))

    result = manager.sync()

    assert result.committed == [1, 2, 3, 4, 5]
    assert backend.completion_order != [1, 2, 3, 4, 5], "тест должен воспроизводить завершение вразнобой"
    log = _git(work_dir, "log", "--format=%s", "--reverse").split("\n")
    assert [line for line in log if line] == [f"Версия {i}" for i in range(1, 6)]
    assert read_version_file(work_dir) == 5


def test_sync_preserves_author_date_and_version_marker(work_dir, tmp_path):
    backend = FakeStorageBackend(_versions(2))
    manager = SyncManager(
        work_dir, backend, SyncOptions(jobs=2, temp_root=tmp_path / "tmp", email_domain="example.org")
    )
    manager.sync()

    entries = _git(
        work_dir, "log", "--format=%an|%ae|%ad|%s", "--date=format:%Y-%m-%d %H:%M:%S", "--reverse"
    ).strip().split("\n")
    assert entries[0] == "Автор1|Автор1@example.org|2026-09-11 12:00:00|Версия 1"
    assert entries[1] == "Автор0|Автор0@example.org|2026-09-12 12:00:00|Версия 2"


def test_authors_file_overrides_signature(work_dir, tmp_path):
    (work_dir / "AUTHORS").write_text("Автор1=Иван Иванов <ivan@corp.local>\n", encoding="utf-8")
    backend = FakeStorageBackend(_versions(1))
    SyncManager(work_dir, backend, SyncOptions(jobs=1, temp_root=tmp_path / "tmp")).sync()

    assert _git(work_dir, "log", "-1", "--format=%an|%ae").strip() == "Иван Иванов|ivan@corp.local"


def test_bounded_pipeline_limits_concurrent_exports(work_dir, tmp_path):
    backend = FakeStorageBackend(_versions(8), delays=dict.fromkeys(range(1, 9), 0.05))
    manager = SyncManager(work_dir, backend, SyncOptions(jobs=2, queue_limit=3, temp_root=tmp_path / "t"))

    result = manager.sync()

    assert backend.max_concurrent <= 2
    # Конвейер не должен «убегать» вперёд: не больше jobs+queue_limit версий в работе одновременно.
    assert result.max_inflight <= 2 + 3
    assert result.committed == list(range(1, 9))


def test_each_version_exports_into_isolated_directory(work_dir, tmp_path):
    backend = FakeStorageBackend(_versions(4))
    SyncManager(work_dir, backend, SyncOptions(jobs=4, temp_root=tmp_path / "tmp")).sync()

    assert len(set(backend.export_dirs.values())) == 4


def test_failure_stops_before_later_commits_and_keeps_version_marker(work_dir, tmp_path):
    backend = FakeStorageBackend(_versions(4), fail_versions={3: RuntimeError("выгрузка упала")})
    manager = SyncManager(work_dir, backend, SyncOptions(jobs=4, retries=1, temp_root=tmp_path / "tmp"))

    result = manager.sync(raise_on_error=False)

    assert result.committed == [1, 2]
    assert result.failed_version == 3
    assert "выгрузка упала" in str(result.error)
    assert read_version_file(work_dir) == 2
    assert [line for line in _git(work_dir, "log", "--format=%s").split("\n") if line] == [
        "Версия 2",
        "Версия 1",
    ]


def test_transient_failure_is_retried(work_dir, tmp_path):
    backend = FakeStorageBackend(_versions(2), flaky_versions={2: 1})
    manager = SyncManager(work_dir, backend, SyncOptions(jobs=2, retries=2, temp_root=tmp_path / "tmp"))

    result = manager.sync()

    assert result.committed == [1, 2]
    assert backend.attempts[2] == 2


def test_resume_after_failure_does_not_duplicate_commits(work_dir, tmp_path):
    backend = FakeStorageBackend(_versions(4), fail_versions={3: RuntimeError("сбой")})
    options = SyncOptions(jobs=2, retries=1, temp_root=tmp_path / "tmp")
    SyncManager(work_dir, backend, options).sync(raise_on_error=False)
    commits_after_failure = GitRepo(work_dir).commit_count()

    healthy = FakeStorageBackend(_versions(4))
    result = SyncManager(work_dir, healthy, options).sync()

    assert result.committed == [3, 4]
    assert healthy.attempts.get(1) is None, "уже синхронизированные версии не выгружаются повторно"
    assert GitRepo(work_dir).commit_count() == commits_after_failure + 2
    subjects = [line for line in _git(work_dir, "log", "--format=%s", "--reverse").split("\n") if line]
    assert subjects == ["Версия 1", "Версия 2", "Версия 3", "Версия 4"]


def test_second_run_without_new_versions_is_noop(work_dir, tmp_path):
    backend = FakeStorageBackend(_versions(3))
    options = SyncOptions(jobs=2, temp_root=tmp_path / "tmp")
    SyncManager(work_dir, backend, options).sync()
    count = GitRepo(work_dir).commit_count()

    result = SyncManager(work_dir, FakeStorageBackend(_versions(3)), options).sync()

    assert result.committed == []
    assert GitRepo(work_dir).commit_count() == count


def test_deleted_objects_disappear_from_working_copy_and_git(work_dir, tmp_path):
    backend = FakeStorageBackend(_versions(2))
    backend.files = {
        1: {"Справочники/Товары.xml": "v1", "Справочники/Удаляемый.xml": "v1"},
        2: {"Справочники/Товары.xml": "v2"},
    }
    SyncManager(work_dir, backend, SyncOptions(jobs=2, temp_root=tmp_path / "tmp")).sync()

    tracked = [line for line in _git(work_dir, "ls-files").split("\n") if line]
    assert "Справочники/Товары.xml" in tracked
    assert "Справочники/Удаляемый.xml" not in tracked
    assert not (work_dir / "Справочники" / "Удаляемый.xml").exists()
    stat = _git(work_dir, "show", "--stat", "--format=", "HEAD")
    assert "Удаляемый.xml" in stat


def test_service_files_survive_cleanup(work_dir, tmp_path):
    (work_dir / "AUTHORS").write_text("Автор1=А1 <a1@e.org>\n", encoding="utf-8")
    (work_dir / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (work_dir / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    backend = FakeStorageBackend(_versions(1))

    SyncManager(work_dir, backend, SyncOptions(jobs=1, temp_root=tmp_path / "tmp")).sync()

    assert (work_dir / "AUTHORS").exists()
    assert (work_dir / ".gitignore").exists()
    assert (work_dir / ".gitattributes").exists()
    assert (work_dir / ".git").is_dir()


def test_cancellation_stops_within_bounded_time(work_dir, tmp_path):
    backend = FakeStorageBackend(_versions(40), delays=dict.fromkeys(range(1, 41), 0.25))
    cancel = threading.Event()
    manager = SyncManager(work_dir, backend, SyncOptions(jobs=4, temp_root=tmp_path / "tmp"))

    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    result = manager.sync(cancel=cancel, raise_on_error=False)
    elapsed = time.monotonic() - started

    assert result.cancelled
    assert elapsed < 6.0, f"отмена заняла {elapsed:.1f} с"
    assert result.committed == sorted(result.committed)
    assert read_version_file(work_dir) == (result.committed[-1] if result.committed else 0)


def test_dirty_working_copy_is_refused(work_dir, tmp_path):
    (work_dir / "ручная правка.txt").write_text("не трогать", encoding="utf-8")
    backend = FakeStorageBackend(_versions(1))

    with pytest.raises(DirtyWorkingCopyError):
        SyncManager(work_dir, backend, SyncOptions(jobs=1, temp_root=tmp_path / "tmp")).sync()

    assert (work_dir / "ручная правка.txt").read_text(encoding="utf-8") == "не трогать"


def test_version_in_git_ahead_of_storage_is_rejected(work_dir, tmp_path):
    from gitsync.version_file import write_version_file

    write_version_file(work_dir, 500)
    GitRepo(work_dir).commit_all("маркер", "И <i@e.org>", dt.datetime(2026, 1, 1))
    backend = FakeStorageBackend(_versions(3))

    with pytest.raises(StorageVersionMismatchError):
        SyncManager(work_dir, backend, SyncOptions(jobs=1, temp_root=tmp_path / "tmp")).sync()


def test_export_escaping_temp_dir_is_rejected(work_dir, tmp_path):
    backend = FakeStorageBackend(_versions(1))
    backend.files = {1: {"../снаружи.xml": "зло"}}

    result = SyncManager(
        work_dir, backend, SyncOptions(jobs=1, retries=1, temp_root=tmp_path / "tmp")
    ).sync(raise_on_error=False)

    assert isinstance(result.error, UnsafePathError)
    assert not (tmp_path / "снаружи.xml").exists()
    assert result.committed == []


def test_symlink_in_export_is_not_followed(work_dir, tmp_path):
    outside = tmp_path / "секреты.txt"
    outside.write_text("секретные данные", encoding="utf-8")
    backend = FakeStorageBackend(_versions(1))
    backend.symlinks = {1: {"ссылка.xml": str(outside)}}

    result = SyncManager(
        work_dir, backend, SyncOptions(jobs=1, retries=1, temp_root=tmp_path / "tmp")
    ).sync(raise_on_error=False)

    assert result.committed == [] or not (work_dir / "ссылка.xml").is_symlink()
    if result.committed:
        assert not (work_dir / "ссылка.xml").exists() or (
            (work_dir / "ссылка.xml").read_text(encoding="utf-8") != "секретные данные"
        )
    assert outside.read_text(encoding="utf-8") == "секретные данные"


def test_resume_does_not_mistake_empty_range_for_recreated_storage(tmp_path):
    """Дошли до версии 20, новых нет — это не «хранилище пересоздали».

    Бэкенд отдаёт историю от запрошенного номера, поэтому при resume отфильтрованный максимум
    равен нулю. На живом стенде это видно в журнале: «максимум в хранилище: 0». Порог
    расхождения нельзя считать по отфильтрованной истории.
    """
    work_dir = tmp_path / "рк"
    backend = FakeStorageBackend(_versions(20))
    options = SyncOptions(jobs=2, temp_root=tmp_path / "tmp")
    SyncManager(work_dir, backend, options).sync()
    assert read_version_file(work_dir) == 20

    result = SyncManager(work_dir, backend, options).sync()

    assert result.committed == []
    assert GitRepo(work_dir).commit_count() == 20


def test_recreated_storage_is_refused_even_when_range_is_empty(tmp_path):
    """Хранилище заменили на короткое — отказ до записи, а не молчаливое «новых версий нет»."""
    work_dir = tmp_path / "рк"
    options = SyncOptions(jobs=2, temp_root=tmp_path / "tmp")
    SyncManager(work_dir, FakeStorageBackend(_versions(20)), options).sync()

    recreated = FakeStorageBackend(_versions(2))
    with pytest.raises(StorageVersionMismatchError):
        SyncManager(work_dir, recreated, options).sync()

    assert GitRepo(work_dir).commit_count() == 20
    assert read_version_file(work_dir) == 20


@pytest.mark.parametrize("message", ["invalid credentials", "Пользователь уже аутентифицирован в хранилище"])
def test_unclassified_native_failure_is_not_retried(work_dir, tmp_path, message):
    from gitsync.errors import DesignerError

    backend = FakeStorageBackend(_versions(1), fail_versions={1: DesignerError(message)})
    manager = SyncManager(work_dir, backend, SyncOptions(jobs=1, retries=3, temp_root=tmp_path / "tmp"))
    result = manager.sync(raise_on_error=False)
    assert isinstance(result.error, DesignerError)
    assert result.committed == []
    assert backend.attempts == {1: 1}
