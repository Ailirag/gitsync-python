"""Слайс 10: три хранилища 1С в ОДНОМ общем репозитории Git.

Исполняемая приёмка пользовательского сценария: основная конфигурация и ДВА разных
расширения выгружаются в подкаталоги «Конфигурация», «Расширение 1», «Расширение 2»
одного репозитория базы. Проверяется через публичный CLI и настоящий git.

Репозиторий в фикстуре создаётся ЧУЖОЙ командой ``git init`` с ``core.quotepath=true``:
в общий репозиторий базы gitsync приходит вторым, а не создаёт его, и обязан работать
с кириллическими именами каталогов при штатном экранировании путей в ``git status``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from gitsync.cli import main
from support.native_report import ReportVersion, build_report_mxl

CONFIG = "Конфигурация"
EXT1 = "Расширение 1"
EXT2 = "Расширение 2"

#: (номер, автор, время, комментарий, файлы версии)
SOURCE_VERSIONS: dict[str, list[tuple[int, str, str, str, dict[str, str]]]] = {
    CONFIG: [
        (1, "Иванов", "10:20:30", "Создание хранилища конфигурации",
         {"Справочники/Товары.xml": '<Товары версия="1"/>',
          "Справочники/Контрагенты.xml": '<Контрагенты версия="1"/>'}),
        (2, "Петров", "11:05:00", "Доработка справочника Товары\nвторая строка комментария",
         {"Справочники/Товары.xml": '<Товары версия="2"/>',
          "Справочники/Контрагенты.xml": '<Контрагенты версия="1"/>'}),
    ],
    EXT1: [
        (1, "Петров", "12:43:43", "extension 1: создание хранилища",
         {"Configuration.xml": '<Расширение имя="GX" версия="1"/>'}),
        (2, "Иванов", "12:44:20", "extension 1: добавлен модуль GX_Probe",
         {"Configuration.xml": '<Расширение имя="GX" версия="2"/>',
          "CommonModules/GX_Probe/Ext/Module.bsl":
          'Функция Маркер() Экспорт\n Возврат "gx-v2";\nКонецФункции\n'}),
    ],
    EXT2: [
        (1, "Сидорова", "13:10:00", "extension 2: создание хранилища",
         {"Configuration.xml": '<Расширение имя="GM" версия="1"/>'}),
        (2, "Кузнецов", "13:20:00", "extension 2: добавлен модуль GM_Audit",
         {"Configuration.xml": '<Расширение имя="GM" версия="2"/>',
          "CommonModules/GM_Audit/Ext/Module.bsl":
          'Функция Аудит() Экспорт\n Возврат "gm-v2";\nКонецФункции\n'}),
    ],
}

TOTAL_VERSIONS = sum(len(rows) for rows in SOURCE_VERSIONS.values())


def _git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _write_storage(root: Path, rows: list[tuple[int, str, str, str, dict[str, str]]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.mxl").write_bytes(build_report_mxl(
        [ReportVersion(number, author, "15.09.2026", moment, comment)
         for number, author, moment, comment, _ in rows]
    ))
    for number, _, _, _, files in rows:
        for relative, text in files.items():
            target = root / f"v{number}" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
    return root


@pytest.fixture()
def storages(tmp_path) -> dict[str, Path]:
    return {name: _write_storage(tmp_path / "хранилища" / name, rows)
            for name, rows in SOURCE_VERSIONS.items()}


@pytest.fixture()
def shared_repo(tmp_path) -> Path:
    """Общий репозиторий базы, созданный НЕ нами: чужой README и чужой подкаталог."""
    repo = tmp_path / "база"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.quotepath", "true")
    (repo / "README.md").write_text("# общий репозиторий базы\n", encoding="utf-8", newline="\n")
    (repo / "Документация").mkdir()
    (repo / "Документация" / "регламент.md").write_text("не трогать\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Оператор", "-c", "user.email=operator@example.org",
         "commit", "-m", "исходное содержимое")
    return repo


def _manifest(path: Path, repo: Path, storages: dict[str, Path], tmp_path: Path,
              subtrees: dict[str, str] | None = None, **defaults) -> Path:
    subtrees = subtrees or {name: name for name in storages}
    config = {
        "repository": str(repo),
        "defaults": {"backend": "fixture", "jobs": 2, "email_domain": "example.org",
                     "init": True, "temp_root": str(tmp_path / "врем"), **defaults},
        "storages": [{"name": name, "subtree": subtrees[name], "fixture_root": str(root)}
                     for name, root in storages.items()],
    }
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _commits(repo: Path) -> list[str]:
    return _git(repo, "rev-list", "--reverse", "HEAD").split()


def _commit_rows(repo: Path) -> list[dict]:
    raw = _git(repo, "log", "--reverse", "--date=format:%Y-%m-%d %H:%M:%S",
               "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%ad%x1f%cd%x1f%B%x1e")
    rows = []
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        sha, an, ae, cn, ce, ad, cd, body = chunk.split("\x1f")
        files = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha)
        rows.append({"sha": sha, "author": an, "email": ae, "committer": cn, "committer_email": ce,
                     "date": ad, "committer_date": cd, "message": body,
                     "files": [name for name in files.split("\0") if name]})
    return rows


def _tree_sha(repo: Path, subtree: str) -> str:
    return _git(repo, "rev-parse", f"HEAD:{subtree}").strip()


def _version(path: Path) -> str:
    return (path / "VERSION").read_text(encoding="utf-8")


# --- M01: три источника в одном репозитории -------------------------------


def test_three_sources_land_in_one_repository_without_nested_git(shared_repo, storages, tmp_path):
    config = _manifest(tmp_path / "монорепо.json", shared_repo, storages, tmp_path)

    assert main(["sync-all", "--config", str(config)]) == 0

    assert [item.relative_to(shared_repo).as_posix() for item in shared_repo.rglob(".git")] == [".git"]
    for name in storages:
        assert (shared_repo / name / "VERSION").is_file()
        assert (shared_repo / name / "AUTHORS").is_file()
        assert f"<VERSION>{len(SOURCE_VERSIONS[name])}</VERSION>" in _version(shared_repo / name)
    assert len(_commits(shared_repo)) == 1 + TOTAL_VERSIONS
    # Чужое содержимое общего репозитория не пострадало.
    assert (shared_repo / "README.md").read_text(encoding="utf-8") == "# общий репозиторий базы\n"
    assert (shared_repo / "Документация" / "регламент.md").is_file()
    status = _git(shared_repo, "status", "--porcelain", "--untracked-files=all")
    assert "README.md" not in status and "регламент" not in status


def test_commits_are_scoped_to_their_source_with_exact_metadata(shared_repo, storages, tmp_path):
    # Своя таблица авторов конкретного источника: подпись коммита берётся из неё.
    (shared_repo / CONFIG).mkdir()
    (shared_repo / CONFIG / "AUTHORS").write_text(
        "// сопоставление для основной конфигурации\n"
        "Иванов=Иван Иванов <ivanov@corp.example>\n", encoding="utf-8", newline="\n")
    config = _manifest(tmp_path / "монорепо.json", shared_repo, storages, tmp_path)

    assert main(["sync-all", "--config", str(config)]) == 0

    rows = _commit_rows(shared_repo)[1:]  # первый коммит — чужое исходное содержимое
    expected = [(name, row) for name, rows_ in SOURCE_VERSIONS.items() for row in rows_]
    assert len(rows) == len(expected)
    for commit, (source, (number, author, moment, comment, files)) in zip(rows, expected, strict=True):
        assert commit["message"].strip("\n") == comment
        assert commit["date"] == f"2026-09-15 {moment}"
        assert commit["committer_date"] == commit["date"]
        if source == CONFIG and author == "Иванов":
            assert (commit["author"], commit["email"]) == ("Иван Иванов", "ivanov@corp.example")
        else:
            assert (commit["author"], commit["email"]) == (author, f"{author}@example.org")
        assert commit["committer"] == commit["author"]
        # Коммит источника трогает только его подкаталог, включая маркер VERSION.
        assert all(name.startswith(source + "/") for name in commit["files"]), commit["files"]
        assert f"{source}/VERSION" in commit["files"]
        for relative in files:
            assert f"{source}/{relative}" in commit["files"] or number > 1


# --- M02: повтор без изменений ---------------------------------------------


def test_repeated_run_is_noop_and_keeps_every_sha(shared_repo, storages, tmp_path):
    config = _manifest(tmp_path / "монорепо.json", shared_repo, storages, tmp_path)
    assert main(["sync-all", "--config", str(config)]) == 0
    before = _commits(shared_repo)
    markers = {name: _version(shared_repo / name) for name in storages}

    assert main(["sync-all", "--config", str(config)]) == 0

    assert before and _commits(shared_repo) == before
    assert {name: _version(shared_repo / name) for name in storages} == markers
    assert not _git(shared_repo, "status", "--porcelain").replace("\n", "").strip().endswith("README.md")


# --- M03: инкремент одного источника ---------------------------------------


def test_increment_of_one_source_leaves_the_others_byte_identical(shared_repo, storages, tmp_path):
    config = _manifest(tmp_path / "монорепо.json", shared_repo, storages, tmp_path)
    assert main(["sync-all", "--config", str(config)]) == 0
    untouched = {name: _tree_sha(shared_repo, name) for name in (EXT1, EXT2)}
    markers = {name: (shared_repo / name / "VERSION").read_bytes() for name in (EXT1, EXT2)}
    root_readme = _git(shared_repo, "rev-parse", "HEAD:README.md").strip()

    # В основной конфигурации появилась версия 3: справочник Контрагенты удалён.
    _write_storage(storages[CONFIG], SOURCE_VERSIONS[CONFIG] + [
        (3, "Сидорова", "14:00:00", "Удалён справочник Контрагенты",
         {"Справочники/Товары.xml": '<Товары версия="3"/>'})])
    assert main(["sync-all", "--config", str(config)]) == 0

    assert "<VERSION>3</VERSION>" in _version(shared_repo / CONFIG)
    assert not (shared_repo / CONFIG / "Справочники" / "Контрагенты.xml").exists()
    assert {name: _tree_sha(shared_repo, name) for name in (EXT1, EXT2)} == untouched
    assert {name: (shared_repo / name / "VERSION").read_bytes() for name in (EXT1, EXT2)} == markers
    assert _git(shared_repo, "rev-parse", "HEAD:README.md").strip() == root_readme
    last = _commit_rows(shared_repo)[-1]
    assert last["files"] == [f"{CONFIG}/Справочники/Контрагенты.xml", f"{CONFIG}/VERSION"] or \
           sorted(last["files"]) == sorted([f"{CONFIG}/Справочники/Контрагенты.xml",
                                            f"{CONFIG}/Справочники/Товары.xml", f"{CONFIG}/VERSION"])


# --- M04: журнал незавершённой транзакции принадлежит источнику ------------


def _crash_plugin(tmp_path: Path) -> Path:
    module = tmp_path / "плагины"
    module.mkdir(exist_ok=True)
    (module / "crash_before_commit.py").write_text(
        "import os\n\n\ndef register(host):\n"
        "    host.subscribe('before_commit', lambda **kwargs: os._exit(9))\n",
        encoding="utf-8")
    return module


def _cli_subprocess(args: list[str], env_extra: dict[str, str] | None = None, timeout: float = 300):
    import os as _os

    env = {**_os.environ, "PYTHONPATH": "", "PYTHONIOENCODING": "utf-8",
           "PYTHONDONTWRITEBYTECODE": "1", **(env_extra or {})}
    return subprocess.run([sys.executable, "-m", "gitsync", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=timeout)


def _sync_args(repo: Path, name: str, storages: dict[str, Path], tmp_path: Path) -> list[str]:
    return ["sync", "--workdir", str(repo / name), "--backend", "fixture",
            "--fixture-root", str(storages[name]), "--jobs", "2",
            "--temp-root", str(tmp_path / "врем"), "--email-domain", "example.org"]


def test_interrupted_source_neither_blocks_nor_reconciles_other_sources(shared_repo, storages, tmp_path):
    config = _manifest(tmp_path / "монорепо.json", shared_repo, storages, tmp_path)
    assert main(["sync-all", "--config", str(config), "--name", CONFIG]) == 0
    for name in (EXT1, EXT2):
        assert main(["init", "--workdir", str(shared_repo / name), "--backend", "fixture",
                     "--fixture-root", str(storages[name]), "--email-domain", "example.org"]) == 0
    # Основная конфигурация получает новую версию и падает ПОСРЕДИ транзакции.
    _write_storage(storages[CONFIG], SOURCE_VERSIONS[CONFIG] + [
        (3, "Сидорова", "14:00:00", "Оборванная версия", {"Справочники/Товары.xml": '<Товары версия="3"/>'})])
    crashed = _cli_subprocess(_sync_args(shared_repo, CONFIG, storages, tmp_path)
                              + ["--plugin", "crash_before_commit"],
                              {"PYTHONPATH": str(_crash_plugin(tmp_path))})
    assert crashed.returncode == 9, crashed.stderr
    journals = sorted((shared_repo / ".git").glob("gitsync-py-journal*.json"))
    assert len(journals) == 1
    journal_before = journals[0].read_bytes()
    assert json.loads(journal_before)["sync_dir"] == str((shared_repo / CONFIG).resolve())
    head_before = _git(shared_repo, "rev-parse", "HEAD").strip()

    # Порядок источников изменён: оборванная транзакция чужого источника не мешает и не «чинится».
    for name in (EXT2, EXT1):
        result = _cli_subprocess(_sync_args(shared_repo, name, storages, tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"<VERSION>{len(SOURCE_VERSIONS[name])}</VERSION>" in _version(shared_repo / name)
    assert sorted((shared_repo / ".git").glob("gitsync-py-journal*.json")) == journals
    assert journals[0].read_bytes() == journal_before
    assert _git(shared_repo, "rev-parse", f"{head_before}^{{commit}}").strip() == head_before

    # Источник сам снимает СВОЮ незавершённую транзакцию и досинхронизируется.
    recovered = _cli_subprocess(_sync_args(shared_repo, CONFIG, storages, tmp_path))
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert "<VERSION>3</VERSION>" in _version(shared_repo / CONFIG)
    assert not list((shared_repo / ".git").glob("gitsync-py-journal*.json"))
    subjects = [row["message"].strip() for row in _commit_rows(shared_repo)]
    assert subjects.count("Оборванная версия") == 1
    assert len(_commits(shared_repo)) == 1 + TOTAL_VERSIONS + 1


# --- M05: общая каноническая блокировка ------------------------------------


def test_lock_is_canonical_for_every_source_of_the_repository(shared_repo, storages, tmp_path):
    from gitsync.sync import resolve_lock_path

    for name in storages:
        (shared_repo / name).mkdir(exist_ok=True)
    paths = {resolve_lock_path(shared_repo / name) for name in storages}
    assert paths == {shared_repo / ".git" / "gitsync-py.lock"}


def test_busy_repository_fails_cleanly_and_waiting_run_succeeds(shared_repo, storages, tmp_path):
    from gitsync.locks import exclusive_lock
    from gitsync.sync import resolve_lock_path

    for name in storages:
        assert main(["init", "--workdir", str(shared_repo / name), "--backend", "fixture",
                     "--fixture-root", str(storages[name]), "--email-domain", "example.org"]) == 0
    lock_path = resolve_lock_path(shared_repo / CONFIG)

    with exclusive_lock(lock_path, timeout=5):
        busy = _cli_subprocess(_sync_args(shared_repo, EXT1, storages, tmp_path) + ["--lock-timeout", "0"])
    assert busy.returncode != 0
    assert "уже обрабатывается" in (busy.stdout + busy.stderr)
    assert "версии None" not in (busy.stdout + busy.stderr)
    assert len(_commits(shared_repo)) == 1

    holder_released = threading.Event()

    def hold():
        with exclusive_lock(lock_path, timeout=5):
            time.sleep(1.0)
        holder_released.set()

    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.2)
    waited = _cli_subprocess(_sync_args(shared_repo, EXT1, storages, tmp_path) + ["--lock-timeout", "30"])
    thread.join(timeout=30)
    assert holder_released.is_set()
    assert waited.returncode == 0, waited.stdout + waited.stderr
    assert f"<VERSION>{len(SOURCE_VERSIONS[EXT1])}</VERSION>" in _version(shared_repo / EXT1)


def test_parallel_invocations_on_one_repository_do_not_duplicate_or_lose_commits(
        shared_repo, storages, tmp_path):
    for name in storages:
        assert main(["init", "--workdir", str(shared_repo / name), "--backend", "fixture",
                     "--fixture-root", str(storages[name]), "--email-domain", "example.org"]) == 0
    import os as _os

    env = {**_os.environ, "PYTHONPATH": "", "PYTHONIOENCODING": "utf-8"}
    processes = [subprocess.Popen(
        [sys.executable, "-m", "gitsync",
         *_sync_args(shared_repo, name, storages, tmp_path), "--lock-timeout", "120"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=env)
        for name in storages]
    outputs = [(process.wait(timeout=300), process.communicate()[0]) for process in processes]

    assert all(code == 0 for code, _ in outputs), outputs
    commits = _commits(shared_repo)
    assert len(commits) == 1 + TOTAL_VERSIONS
    messages = [row["message"].strip() for row in _commit_rows(shared_repo)]
    assert len(messages) == len(set(messages))
    for name in storages:
        assert f"<VERSION>{len(SOURCE_VERSIONS[name])}</VERSION>" in _version(shared_repo / name)


# --- M06: неоднозначные и перекрывающиеся источники ------------------------


def test_overlapping_subtrees_are_rejected_before_any_work(shared_repo, storages, tmp_path):
    config = _manifest(tmp_path / "перекрытие.json", shared_repo, storages, tmp_path,
                       subtrees={CONFIG: CONFIG, EXT1: f"{CONFIG}/Расширение", EXT2: EXT2})

    code = main(["sync-all", "--config", str(config)])

    assert code == 2
    assert len(_commits(shared_repo)) == 1
    assert not (shared_repo / CONFIG).exists()


def test_duplicate_workdir_in_manifest_is_rejected(shared_repo, storages, tmp_path):
    config = _manifest(tmp_path / "дубль.json", shared_repo, storages, tmp_path,
                       subtrees={CONFIG: CONFIG, EXT1: CONFIG, EXT2: EXT2})

    assert main(["sync-all", "--config", str(config)]) == 2
    assert len(_commits(shared_repo)) == 1


def test_source_nested_inside_another_source_is_refused(shared_repo, storages, tmp_path):
    config = _manifest(tmp_path / "монорепо.json", shared_repo, storages, tmp_path)
    assert main(["sync-all", "--config", str(config)]) == 0

    nested = shared_repo / CONFIG / "Вложенный"
    nested.mkdir(parents=True)
    result = _cli_subprocess(["sync", "--workdir", str(nested), "--backend", "fixture",
                              "--fixture-root", str(storages[EXT1]), "--jobs", "1",
                              "--temp-root", str(tmp_path / "врем")])

    assert result.returncode != 0
    assert len(_commits(shared_repo)) == 1 + TOTAL_VERSIONS


# --- M07: чужие правки в общем репозитории ---------------------------------


def test_foreign_change_outside_subtree_does_not_block_the_source(shared_repo, storages, tmp_path):
    config = _manifest(tmp_path / "монорепо.json", shared_repo, storages, tmp_path)
    assert main(["sync-all", "--config", str(config)]) == 0
    (shared_repo / "README.md").write_text("# правка оператора\n", encoding="utf-8", newline="\n")
    (shared_repo / "черновик.txt").write_text("временный файл\n", encoding="utf-8", newline="\n")
    _write_storage(storages[EXT2], SOURCE_VERSIONS[EXT2] + [
        (3, "Сидорова", "15:00:00", "extension 2: правка модуля",
         {"Configuration.xml": '<Расширение имя="GM" версия="3"/>',
          "CommonModules/GM_Audit/Ext/Module.bsl":
          'Функция Аудит() Экспорт\n Возврат "gm-v3";\nКонецФункции\n'})])

    result = _cli_subprocess(_sync_args(shared_repo, EXT2, storages, tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "<VERSION>3</VERSION>" in _version(shared_repo / EXT2)
    # Чужие правки не тронуты и не закоммичены.
    assert (shared_repo / "README.md").read_text(encoding="utf-8") == "# правка оператора\n"
    assert (shared_repo / "черновик.txt").is_file()
    assert _commit_rows(shared_repo)[-1]["files"] == [
        f"{EXT2}/CommonModules/GM_Audit/Ext/Module.bsl", f"{EXT2}/Configuration.xml", f"{EXT2}/VERSION"]


def test_foreign_change_inside_subtree_still_blocks_the_source(shared_repo, storages, tmp_path):
    config = _manifest(tmp_path / "монорепо.json", shared_repo, storages, tmp_path)
    assert main(["sync-all", "--config", str(config)]) == 0
    (shared_repo / EXT1 / "Configuration.xml").write_text("ручная правка", encoding="utf-8")
    _write_storage(storages[EXT1], SOURCE_VERSIONS[EXT1] + [
        (3, "Петров", "16:00:00", "extension 1: третья версия",
         {"Configuration.xml": '<Расширение имя="GX" версия="3"/>'})])

    result = _cli_subprocess(_sync_args(shared_repo, EXT1, storages, tmp_path))

    assert result.returncode != 0
    assert (shared_repo / EXT1 / "Configuration.xml").read_text(encoding="utf-8") == "ручная правка"
    assert "<VERSION>2</VERSION>" in _version(shared_repo / EXT1)


# --- M08: параллельная выгрузка внутри источника ---------------------------


def test_source_inside_shared_repository_still_exports_in_parallel(shared_repo, tmp_path):
    import datetime as dt

    from gitsync.backends import FakeStorageBackend
    from gitsync.storage_report import StorageVersion
    from gitsync.sync import SyncManager, SyncOptions

    versions = [StorageVersion(number, "Иванов", dt.datetime(2026, 9, 15, 10, number), f"версия {number}")
                for number in range(1, 5)]
    backend = FakeStorageBackend(versions, delays=dict.fromkeys(range(1, 5), 0.3))
    work = shared_repo / CONFIG
    work.mkdir()
    manager = SyncManager(work, backend, SyncOptions(jobs=2, temp_root=tmp_path / "врем"))

    result = manager.sync()

    assert result.committed == [1, 2, 3, 4]
    assert backend.max_concurrent >= 2
    assert manager.repo.path == shared_repo
