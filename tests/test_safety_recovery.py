"""Регрессии по дефектам независимого review d6ccf84 (F01–F13).

Каждый тест закрывает конкретную находку и падал на коде до remediation. Проверки идут на
настоящем git и настоящих дочерних процессах; 1С/сеть/секреты не используются — только
синтетические канарейки и фикстуры. Названия ссылаются на номер находки, чтобы повторный
review мог сопоставить их с отчётом.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import gitsync
from gitsync.backends import FakeStorageBackend
from gitsync.cli import build_parser, main
from gitsync.designer import DesignerRunner
from gitsync.errors import (
    DesignerError,
    ExportIncompleteError,
    LockBusyError,
    PostCommitError,
    UnsafePathError,
    VersionFileError,
)
from gitsync.gitrepo import GitRepo
from gitsync.locks import exclusive_lock
from gitsync.plugins import PluginHost
from gitsync.storage_report import StorageVersion
from gitsync.sync import LOCK_FILE_NAME, SyncManager, SyncOptions
from gitsync.version_file import read_version_file, write_version_file

#: Каталог, из которого импортируется ПРОВЕРЯЕМЫЙ пакет: дочерние процессы обязаны
#: брать тот же код, что и тест. Вычисляется по самому модулю, а не по раскладке
#: дерева: при установке колеса (в том числе в контейнере) исходников рядом нет.
SRC = Path(gitsync.__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "fixture-storage"


def _versions(*numbers: int) -> list[StorageVersion]:
    return [StorageVersion(n, "Автор", dt.datetime(2026, 1, 1), f"v{n}") for n in numbers]


def _backend(*numbers: int) -> FakeStorageBackend:
    backend = FakeStorageBackend(_versions(*numbers))
    backend.files = {n: {"object.txt": f"v{n}"} for n in numbers}
    return backend


def _seed(path: Path, marker: int = 0, files: dict[str, str] | None = None) -> GitRepo:
    repo = GitRepo(path)
    repo.init()
    for name, content in (files or {}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    write_version_file(path, marker)
    repo.commit_all("seed", "Тест <test@example.invalid>", dt.datetime(2026, 1, 1))
    return repo


def _manager(path: Path, backend, **kwargs) -> SyncManager:
    return SyncManager(path, backend, SyncOptions(jobs=1, retries=0, **kwargs))


# --- F01: владение временным каталогом ------------------------------------


def test_f01_cleanup_keeps_unowned_files_in_shared_temp_root(tmp_path):
    shared = tmp_path / "shared-temp"
    shared.mkdir()
    sentinel = shared / "unrelated.txt"
    sentinel.write_text("чужой файл", encoding="utf-8")

    result = _manager(tmp_path / "repo", _backend(1), temp_root=shared).sync()

    assert result.ok
    assert sentinel.exists(), "очистка удалила чужой файл из общего корня временных каталогов"
    assert not any(item.name.startswith("run-") for item in shared.iterdir()), "свой run-каталог не убран"


def test_f01_parallel_repositories_do_not_delete_each_other_export(tmp_path):
    first, second = tmp_path / "repoA", tmp_path / "repoB"
    _seed(first, 0, {"object.txt": "старый A"})
    _seed(second, 0, {"object.txt": "старый B"})
    ready, release = threading.Event(), threading.Event()

    class Paused(FakeStorageBackend):
        def export_version(self, version, dest, cancel=None):
            super().export_version(version, dest, cancel)
            ready.set()
            assert release.wait(30), "координация теста не сработала"

    slow = Paused(_versions(1))
    slow.files = {1: {"object.txt": "новый B"}}
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(_manager(second, slow).sync)
        try:
            assert ready.wait(30)
            assert _manager(first, _backend(1)).sync().ok
        finally:
            release.set()
        result = future.result(timeout=30)

    assert result.committed == [1]
    assert (second / "object.txt").read_text(encoding="utf-8") == "новый B"


def test_f01_temp_root_inside_working_copy_is_refused(tmp_path):
    work = tmp_path / "repo"
    _seed(work)
    result = _manager(work, _backend(1), temp_root=work / "tmp").sync(raise_on_error=False)
    assert isinstance(result.error, UnsafePathError)


# --- F02: подтверждение выгрузки ------------------------------------------


def test_f02_missing_export_directory_is_not_an_empty_snapshot(tmp_path):
    work = tmp_path / "repo"
    repo = _seed(work, 0, {"object.txt": "сохранить"})
    head = repo.head_sha()

    class NoArtifact(FakeStorageBackend):
        def export_version(self, version, dest, cancel=None):
            return None

    result = _manager(work, NoArtifact(_versions(1))).sync(raise_on_error=False)

    assert isinstance(result.error, ExportIncompleteError)
    assert repo.head_sha() == head
    assert (work / "object.txt").read_text(encoding="utf-8") == "сохранить"


def test_f02_empty_export_is_refused_unless_backend_allows_it(tmp_path):
    class Empty(FakeStorageBackend):
        def export_version(self, version, dest, cancel=None):
            dest.mkdir(parents=True, exist_ok=True)

    work = tmp_path / "repo"
    _seed(work, 0, {"object.txt": "сохранить"})
    result = _manager(work, Empty(_versions(1))).sync(raise_on_error=False)
    assert isinstance(result.error, ExportIncompleteError)

    allowed = Empty(_versions(1))
    allowed.allows_empty_export = True
    second = tmp_path / "repo2"
    _seed(second, 0, {"object.txt": "будет удалён легитимно"})
    assert _manager(second, allowed).sync().committed == [1]


# --- F03: служебные пути Git из внешней выгрузки --------------------------


@pytest.mark.parametrize("name", [".git/config", ".GIT/config", ".git./config", "git~1/config"])
def test_f03_snapshot_cannot_write_git_control_files(tmp_path, name):
    work = tmp_path / "repo"
    repo = _seed(work)
    original = (work / ".git" / "config").read_bytes()
    backend = _backend(1)
    backend.files[1][name] = "[review]\n injected = true\n"

    result = _manager(work, backend).sync(raise_on_error=False)

    assert isinstance(result.error, UnsafePathError)
    assert (work / ".git" / "config").read_bytes() == original
    assert repo.run(["config", "--get", "review.injected"], check=False).returncode != 0


# --- F04/F05: транзакция, журнал и откат своих записей --------------------


def test_f04_crash_between_marker_and_commit_replays_version(tmp_path):
    work = tmp_path / "repo"
    _seed(work, 1, {"object.txt": "одинаковый"})
    code = (
        "import os, sys, datetime as dt\n"
        "from pathlib import Path\n"
        "from gitsync.backends import FakeStorageBackend\n"
        "from gitsync.storage_report import StorageVersion\n"
        "from gitsync.sync import SyncManager, SyncOptions\n"
        "b=FakeStorageBackend([StorageVersion(2,'Автор',dt.datetime(2026,1,1),'v2')])\n"
        "b.files={2:{'object.txt':'одинаковый'}}\n"
        "m=SyncManager(Path(sys.argv[1]),b,SyncOptions(jobs=1,retries=0))\n"
        "m.repo.commit_all=lambda **kw: os._exit(73)\n"
        "m.sync()\n"
    )
    child = subprocess.run([sys.executable, "-c", code, str(work)], cwd=SRC, capture_output=True)
    assert child.returncode == 73, child.stderr.decode("utf-8", "replace")
    assert read_version_file(work) == 2, "маркер намерения должен быть записан до коммита"

    manager = _manager(work, _backend(2))
    manager.backend.files[2] = {"object.txt": "одинаковый"}
    result = manager.sync()

    assert result.rolled_back == [2], "незавершённая транзакция не распознана по журналу"
    assert result.committed == [2]
    assert "<VERSION>2</VERSION>" in manager.repo.run(["show", "HEAD:VERSION"]).stdout


def test_f05_commit_failure_rolls_back_own_writes_and_resume_works(tmp_path):
    work = tmp_path / "repo"
    _seed(work, 0, {"object.txt": "старый"})
    manager = _manager(work, _backend(1))

    def fail(**kwargs):
        raise RuntimeError("сбой git commit (инъекция теста)")

    manager.repo.commit_all = fail
    result = manager.sync(raise_on_error=False)

    assert result.error is not None
    assert manager.repo.run(["status", "--porcelain"]).stdout.strip() == "", "остались свои грязные записи"
    assert (work / "object.txt").read_text(encoding="utf-8") == "старый"
    # Восстановление по умолчанию, без --allow-dirty.
    assert _manager(work, _backend(1)).sync().committed == [1]


def test_f05_rollback_keeps_external_untracked_file(tmp_path):
    work = tmp_path / "repo"
    _seed(work, 0, {"object.txt": "старый"})
    outsider = work / "чужой.txt"
    outsider.write_text("чужие данные", encoding="utf-8")
    outsider_bytes = outsider.read_bytes()
    manager = _manager(work, _backend(1), allow_dirty=True)
    before_head = manager.repo.head_sha()
    before_index = manager.repo.run(["ls-files", "--stage", "-z"]).stdout
    tracked = manager.repo.run(["ls-files", "-z"]).stdout.split("\0")
    before_files = {name: (work / name).read_bytes() for name in tracked if name}
    before_status = manager.repo.run(["status", "--porcelain", "-z"]).stdout

    def fail(**kwargs):
        raise RuntimeError("сбой git commit (инъекция теста)")

    manager.repo.commit_all = fail
    manager.sync(raise_on_error=False)

    # Review-02 R01: a clean status would require deleting the pre-existing outsider.
    # Compare actual bytes and full tracked/index prestate, not absence of user data.
    assert outsider.read_bytes() == outsider_bytes
    assert manager.repo.head_sha() == before_head
    assert manager.repo.run(["ls-files", "--stage", "-z"]).stdout == before_index
    assert {name: (work / name).read_bytes() for name in before_files} == before_files
    assert manager.repo.run(["status", "--porcelain", "-z"]).stdout == before_status


# --- F06: ошибка после коммита не откатывает маркер -----------------------


def test_f06_after_commit_failure_keeps_commit_and_marker(tmp_path):
    work = tmp_path / "repo"
    host = PluginHost(strict=True)

    def boom(**kwargs):
        raise RuntimeError("сбой обработчика после коммита (инъекция теста)")

    host.subscribe("after_commit", boom)
    manager = _manager(work, _backend(1))
    manager.plugins = host

    result = manager.sync(raise_on_error=False)

    assert isinstance(result.error, PostCommitError) and result.post_commit
    assert result.committed == [1], "успешный коммит должен остаться в отчёте"
    assert read_version_file(work) == 1
    assert "<VERSION>1</VERSION>" in manager.repo.run(["show", "HEAD:VERSION"]).stdout


# --- F07: подкаталог src и настоящий корень Git ---------------------------


def test_f07_auto_src_layout_preserves_root_files(tmp_path):
    work = tmp_path / "repo"
    repo = _seed(work, 0, {
        "README.md": "оставить",
        "sibling/note.txt": "оставить",
        "src/VERSION": "<VERSION>1</VERSION>",
        "src/AUTHORS": "Автор=Автор <a@example.invalid>\n",
        "src/object.txt": "v1",
    })
    (work / "VERSION").unlink()
    repo.commit_all("раскладка", "Тест <test@example.invalid>")

    result = _manager(work, _backend(1, 2)).sync()

    assert result.committed == [2]
    assert (work / "README.md").read_text(encoding="utf-8") == "оставить"
    assert (work / "sibling" / "note.txt").exists()
    assert read_version_file(work / "src") == 2
    assert (work / "src" / "object.txt").read_text(encoding="utf-8") == "v2"


def test_f07_explicit_subdir_reuses_parent_repository(tmp_path):
    work = tmp_path / "repo"
    _seed(work, 0, {"src/VERSION": "<VERSION>1</VERSION>", "src/object.txt": "v1"})

    manager = _manager(work / "src", _backend(2))
    result = manager.sync(raise_on_error=False)

    assert not (work / "src" / ".git").exists(), "создан вложенный репозиторий вместо корневого"
    assert manager.repo.path.resolve() == work.resolve()
    assert result.committed == [2]
    assert read_version_file(work / "src") == 2


def test_f07_disable_auto_src_keeps_workdir(tmp_path):
    work = tmp_path / "repo"
    _seed(work, 0, {"src/VERSION": "<VERSION>1</VERSION>"})
    manager = _manager(work, _backend(1), disable_auto_src=True)
    manager.sync()
    assert read_version_file(work) == 1
    # Каталог src при явном запрете авто-src — обычные данные рабочей копии: выгрузка его
    # заменяет, поэтому старый маркер внутри него исчезает вместе с каталогом.
    assert not (work / "src").exists()


# --- F08: отсутствующий VERSION не запускает повтор истории ---------------


def test_f08_missing_version_in_repository_with_history_fails_closed(tmp_path):
    work = tmp_path / "repo"
    manager = _manager(work, _backend(1, 2))
    manager.sync()
    before = manager.repo.commit_count()
    (work / "VERSION").unlink()

    result = _manager(work, _backend(1, 2)).sync(raise_on_error=False)

    assert isinstance(result.error, VersionFileError)
    assert manager.repo.commit_count() == before


def test_f08_corrupt_version_is_not_read_as_zero(tmp_path):
    work = tmp_path / "repo"
    _seed(work, 1)
    (work / "VERSION").write_text("<VERSION>мусор</VERSION>", encoding="utf-8")
    result = _manager(work, _backend(1, 2)).sync(raise_on_error=False)
    assert isinstance(result.error, VersionFileError)


def test_f08_empty_working_copy_still_starts_from_zero(tmp_path):
    assert _manager(tmp_path / "repo", _backend(1)).sync().committed == [1]


# --- F11: блокировки -------------------------------------------------------


def test_f11_lock_recovers_after_owner_crash(tmp_path):
    lock = tmp_path / "lock"
    code = (
        "import os, sys\n"
        "from gitsync.locks import exclusive_lock\n"
        "with exclusive_lock(sys.argv[1]): os._exit(71)\n"
    )
    child = subprocess.run([sys.executable, "-c", code, str(lock)], cwd=SRC, capture_output=True)
    assert child.returncode == 71
    assert lock.exists(), "файл блокировки — носитель блокировки ОС, он остаётся"
    with exclusive_lock(lock, timeout=0.05):
        pass


def test_f11_live_owner_still_refuses_second_process(tmp_path):
    lock = tmp_path / "lock"
    code = (
        "import sys\n"
        "from gitsync.locks import exclusive_lock\n"
        "from gitsync.errors import LockBusyError\n"
        "try:\n"
        "    with exclusive_lock(sys.argv[1],timeout=0):\n"
        "        sys.exit(9)\n"
        "except LockBusyError:\n"
        "    sys.exit(0)\n"
    )
    with exclusive_lock(lock):
        child = subprocess.run([sys.executable, "-c", code, str(lock)], cwd=SRC, capture_output=True)
    assert child.returncode == 0


def test_f11_set_version_respects_sync_lock(tmp_path):
    work = tmp_path / "repo"
    _seed(work, 1)
    with exclusive_lock(work / ".git" / LOCK_FILE_NAME):
        code = main(["set-version", "--workdir", str(work), "--version", "99", "--lock-timeout", "0.1"])
    assert code == 2
    assert read_version_file(work) == 1, "set-version записал версию мимо чужой блокировки"


def test_f11_sync_refuses_while_another_writer_holds_lock(tmp_path):
    work = tmp_path / "repo"
    _seed(work, 0)
    with exclusive_lock(work / ".git" / LOCK_FILE_NAME):
        result = _manager(work, _backend(1), lock_timeout=0.1).sync(raise_on_error=False)
    assert isinstance(result.error, LockBusyError)


def test_f11_lock_error_does_not_disclose_foreign_file_contents(tmp_path):
    lock = tmp_path / "lock"
    lock.write_text("СЕКРЕТ_КАНАРЕЙКА_НЕ_ПАРОЛЬ", encoding="utf-8")
    with exclusive_lock(lock):
        with pytest.raises(LockBusyError) as error:
            with exclusive_lock(lock, timeout=0):
                pass
    assert "КАНАРЕЙКА" not in str(error.value)


# --- F12: пакетный режим ---------------------------------------------------


def test_f12_unknown_config_schema_is_refused(tmp_path):
    config = tmp_path / "upstream.json"
    config.write_text(json.dumps({"repositories": [{"name": "x", "workdir": str(tmp_path / "r")}]}),
                      encoding="utf-8")
    assert main(["sync-all", "--config", str(config)]) != 0
    assert not (tmp_path / "r").exists()


def test_f12_disabled_entry_is_not_executed(tmp_path):
    work = tmp_path / "disabled-repo"
    config = tmp_path / "batch.json"
    config.write_text(json.dumps({"storages": [{
        "name": "disabled", "disable": True, "backend": "fixture",
        "fixture_root": str(FIXTURE), "workdir": str(work),
    }]}), encoding="utf-8")

    assert main(["sync-all", "--config", str(config)]) == 0
    assert not work.exists()


def test_f12_name_filter_runs_only_selected_storage(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    config = tmp_path / "batch.json"
    config.write_text(json.dumps({
        "defaults": {"backend": "fixture", "fixture_root": str(FIXTURE), "jobs": 1},
        "storages": [
            {"name": "one", "workdir": str(first)},
            {"name": "two", "workdir": str(second)},
        ],
    }), encoding="utf-8")

    assert main(["sync-all", "--config", str(config), "--name", "one"]) == 0
    assert (first / "VERSION").is_file()
    assert not second.exists()


def test_f12_duplicate_names_and_unknown_keys_are_refused(tmp_path):
    config = tmp_path / "batch.json"
    config.write_text(json.dumps({"storages": [
        {"name": "one", "workdir": str(tmp_path / "a")},
        {"name": "one", "workdir": str(tmp_path / "b")},
    ]}), encoding="utf-8")
    assert main(["sync-all", "--config", str(config)]) != 0

    config.write_text(json.dumps({"storages": [{"name": "one", "workdir": str(tmp_path / "a"),
                                                "опечатка": 1}]}), encoding="utf-8")
    assert main(["sync-all", "--config", str(config)]) != 0


# --- F13: маскировка секретов в выводе дочернего процесса ------------------


def test_f13_designer_error_redacts_password_from_child_output(tmp_path):
    canary = "TEST_DUMMY_CANARY_NOT_A_SECRET"
    runner = DesignerRunner(sys.executable, tmp_path)
    code = "import sys; print(sys.argv[-1], file=sys.stderr); sys.exit(1)"

    with pytest.raises(DesignerError) as error:
        runner.run([sys.executable, "-c", code, "/P", canary])

    assert canary not in str(error.value)
    assert canary not in repr(error.value.__cause__ or "")


def test_f13_registered_secret_is_redacted_from_successful_output(tmp_path):
    canary = "TEST_REGISTERED_CANARY"
    runner = DesignerRunner(sys.executable, tmp_path)
    runner.secrets.append(canary)
    result = runner.run([sys.executable, "-c", "print('пароль: TEST_REGISTERED_CANARY')"])
    assert canary not in result.stdout


# --- F14 (частично): опции CLI --------------------------------------------


@pytest.mark.parametrize("argv", [
    ["set-version", "--workdir", "unused", "--version", "1", "--commit"],
    ["sync", "--workdir", "unused", "--disable-auto-src"],
    ["sync-all", "--config", "unused", "--name", "x"],
])
def test_f14_core_cli_options_exist(argv):
    build_parser().parse_args(argv)


def test_f14_set_version_commit_creates_commit(tmp_path):
    work = tmp_path / "repo"
    _seed(work, 1)
    assert main(["set-version", "--workdir", str(work), "--version", "42", "--commit"]) == 0
    repo = GitRepo(work)
    assert "<VERSION>42</VERSION>" in repo.run(["show", "HEAD:VERSION"]).stdout
    assert repo.is_clean()
