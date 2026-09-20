"""Слайс 12: переносимость на Linux/в контейнер.

Тесты проверяют поведение, которое ломается именно при запуске в контейнере:
произвольный UID без записи в ``/etc/passwd``, общий каталог блокировок между
контейнерами, манифест, созданный PowerShell на Windows (UTF-8 с BOM), подкаталоги
источников, различающиеся только регистром, и отказ Git работать с каталогом чужого
владельца (bind-монтирование).

Граница моделирования обозначена явно: настоящий конфигуратор 1С здесь не запускается,
а отказ Git по владельцу воспроизводится подменой ``GitRepo.run`` — это *шов*, а не
доказательство поведения Linux. Настоящая проверка того же отказа выполняется в
контейнере, см. ``docs/docker-guide.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import pytest

from gitsync.cli import _cmd_sync_all, build_parser, main
from gitsync.designer import StorageAccess
from gitsync.errors import ConfigError
from gitsync.repository_session import SESSION_DIR_ENV, repository_session_path
from gitsync.sync import discover_repo_root
from support.native_report import ReportVersion, build_report_mxl

ACCESS = StorageAccess(path="tcp://storage-host/база", user="gitsync", password="секрет")


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False, timeout=120)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _fixture_storage(root: Path, versions: int) -> Path:
    """Фикстура хранилища на ``versions`` версий (0 — хранилище без единой версии)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.mxl").write_bytes(build_report_mxl([
        ReportVersion(number=number, author="Иванов", date="16.09.2026",
                      time=f"10:{number:02d}:00", comment=f"версия {number}")
        for number in range(1, versions + 1)
    ]))
    for number in range(1, versions + 1):
        target = root / f"v{number}" / "Справочники" / "Товары.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'<Товары версия="{number}"/>', encoding="utf-8", newline="\n")
    return root


# --- D01: каталог сессий хранилища не зависит от HOME контейнера --------------


def test_session_dir_env_overrides_home(tmp_path, monkeypatch):
    """Общий том блокировок задаётся явно: у контейнеров разных compose-проектов
    HOME различается, и admission разъехался бы на независимые пространства."""
    shared = tmp_path / "сессии"
    monkeypatch.setenv(SESSION_DIR_ENV, str(shared))
    monkeypatch.setenv("HOME", str(tmp_path / "дом-контейнера"))

    path = repository_session_path(ACCESS)

    assert path.parent == shared
    assert path.suffix == ".lock"


def test_session_dir_env_is_the_same_file_for_every_worker_home(tmp_path, monkeypatch):
    """Одно хранилище + один логин = один файл блокировки, даже при разных HOME."""
    shared = tmp_path / "сессии"
    monkeypatch.setenv(SESSION_DIR_ENV, str(shared))

    monkeypatch.setenv("HOME", str(tmp_path / "дом-1"))
    first = repository_session_path(ACCESS)
    monkeypatch.setenv("HOME", str(tmp_path / "дом-2"))
    second = repository_session_path(ACCESS)

    assert first == second


def test_session_dir_must_be_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv(SESSION_DIR_ENV, "сессии")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError) as info:
        repository_session_path(ACCESS)

    assert SESSION_DIR_ENV in str(info.value)


def test_session_path_without_home_fails_closed(monkeypatch):
    """Произвольный ``--user 12345:0`` без записи в /etc/passwd и без HOME.

    Python в такой среде не может определить домашний каталог. Без явного отказа
    инструмент создал бы каталог с именем ``~`` в текущем каталоге или упал бы
    трассировкой, непонятной администратору.
    """
    monkeypatch.delenv(SESSION_DIR_ENV, raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda path: path)

    with pytest.raises(ConfigError) as info:
        repository_session_path(ACCESS)

    message = str(info.value)
    assert SESSION_DIR_ENV in message
    assert "HOME" in message


def test_session_dir_that_cannot_be_created_reports_actionable_error(tmp_path, monkeypatch):
    """Том подмонтирован только для чтения или принадлежит другому UID."""
    blocker = tmp_path / "занято"
    blocker.write_text("это файл, а не каталог", encoding="utf-8")
    monkeypatch.setenv(SESSION_DIR_ENV, str(blocker / "сессии"))

    with pytest.raises(ConfigError) as info:
        repository_session_path(ACCESS)

    assert SESSION_DIR_ENV in str(info.value)


# --- D02: манифест из PowerShell (UTF-8 с BOM) --------------------------------


def test_manifest_written_with_utf8_bom_is_accepted(tmp_path):
    """``Set-Content``/``Out-File`` в Windows PowerShell 5.1 пишут UTF-8 с BOM.

    Такой манифест переносится на Linux как есть; отказ «не является корректным JSON»
    указывал бы администратору на несуществующую ошибку в его файле.
    """
    repo = tmp_path / "база"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    storage = _fixture_storage(tmp_path / "хранилище", versions=2)
    config = {
        "repository": str(repo),
        "defaults": {"backend": "fixture", "init": True, "temp_root": str(tmp_path / "врем")},
        "storages": [{"name": "Конфигурация", "subtree": "Конфигурация",
                      "fixture_root": str(storage)}],
    }
    manifest = tmp_path / "монорепо.json"
    manifest.write_bytes("﻿".encode() + json.dumps(config, ensure_ascii=False).encode("utf-8"))

    assert main(["sync-all", "--config", str(manifest)]) == 0
    assert "<VERSION>2</VERSION>" in (repo / "Конфигурация" / "VERSION").read_text(encoding="utf-8")


# --- D03: подкаталоги, различающиеся только регистром --------------------------


def test_subtrees_differing_only_by_case_are_rejected(tmp_path):
    """На ext4 это два разных каталога, на Windows и в bind-монтировании — один.

    Молча принять такой манифест значит собрать историю, которую нельзя выгрузить
    на машине разработчика: файлы двух источников схлопнутся в один каталог.
    """
    repo = tmp_path / "база"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    storage = _fixture_storage(tmp_path / "хранилище", versions=1)
    config = {
        "repository": str(repo),
        "defaults": {"backend": "fixture", "temp_root": str(tmp_path / "врем")},
        "storages": [
            {"name": "Первый", "subtree": "Расширение", "fixture_root": str(storage)},
            {"name": "Второй", "subtree": "расширение", "fixture_root": str(storage)},
        ],
    }
    manifest = tmp_path / "монорепо.json"
    manifest.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    assert main(["sync-all", "--config", str(manifest)]) == 2
    assert not (repo / "Расширение").exists()
    assert not (repo / "расширение").exists()


def test_case_collision_message_names_both_sources(tmp_path):
    repo = tmp_path / "база"
    repo.mkdir()
    storage = _fixture_storage(tmp_path / "хранилище", versions=1)
    config = {
        "repository": str(repo),
        "defaults": {"backend": "fixture"},
        "storages": [
            {"name": "Первый", "subtree": "Расширение ПЕЧАТИ", "fixture_root": str(storage)},
            {"name": "Второй", "subtree": "Расширение печати", "fixture_root": str(storage)},
        ],
    }
    manifest = tmp_path / "монорепо.json"
    manifest.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigError) as info:
        _cmd_sync_all(argparse.Namespace(config=str(manifest), name=None, log_level="INFO"))

    message = str(info.value)
    assert "Первый" in message and "Второй" in message
    assert "регистр" in message.lower()


# --- D04: отказ Git по владельцу каталога не должен деградировать молча --------


def test_dubious_ownership_is_reported_instead_of_a_nested_repository(tmp_path, monkeypatch):
    """Шов: ``git rev-parse`` подменён отказом, который Git выдаёт на чужом bind-монтировании.

    Без явного отказа ``discover_repo_root`` вернул бы сам подкаталог, и инструмент
    создал бы ВЛОЖЕННЫЙ ``.git`` внутри общего репозитория базы вместо работы с ним.
    """
    from gitsync.gitrepo import GitRepo

    repo = tmp_path / "repo"
    (repo / "Конфигурация").mkdir(parents=True)
    (repo / ".git").mkdir()

    class Refused:
        returncode = 128
        stdout = ""
        stderr = (f"fatal: detected dubious ownership in repository at '{repo}'\n"
                  f"To add an exception for this directory, call:\n"
                  f"\tgit config --global --add safe.directory {repo}\n")

    monkeypatch.setattr(GitRepo, "run", lambda self, *a, **k: Refused())

    with pytest.raises(ConfigError) as info:
        discover_repo_root(repo / "Конфигурация")

    message = str(info.value)
    assert "владел" in message.lower()
    assert str(repo) in message
    # Подсказка ведёт к КОНКРЕТНОМУ каталогу, а не к разрешению «доверять всему подряд».
    assert "safe.directory" in message
    assert "safe.directory *" not in message


# --- D05: остановка оператора в пакетном режиме -------------------------------


def _batch_manifest(tmp_path: Path, names: list[str]) -> Path:
    repo = tmp_path / "база"
    repo.mkdir(exist_ok=True)
    storage = _fixture_storage(tmp_path / "хранилище", versions=1)
    config = {
        "repository": str(repo),
        "defaults": {"backend": "fixture", "temp_root": str(tmp_path / "врем"),
                     "fixture_root": str(storage)},
        "storages": [{"name": name, "subtree": name} for name in names],
    }
    manifest = tmp_path / "монорепо.json"
    manifest.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return manifest


def test_operator_stop_does_not_start_the_next_storage(tmp_path, monkeypatch):
    """``docker stop`` во время sync-all не должен запускать следующее хранилище.

    Шов: ``_cmd_sync`` возвращает код отмены (130) — ровно то, что он возвращает
    после SIGTERM. Проверяется решение пакетного режима, а не доставка сигнала;
    настоящий сигнал проверяется в контейнере, см. docs/docker-guide.md.
    """
    manifest = _batch_manifest(tmp_path, ["Первый", "Второй", "Третий"])
    started: list[str] = []

    def stop_on_first(args):
        started.append(args.workdir)
        return 130

    monkeypatch.setattr("gitsync.cli._cmd_sync", stop_on_first)

    code = _cmd_sync_all(argparse.Namespace(config=str(manifest), name=None, log_level="INFO"))

    assert code == 130
    assert len(started) == 1, "после остановки оператором следующий источник не запускают"


def test_real_failure_outranks_operator_stop(tmp_path, monkeypatch):
    """Если источник действительно сломался, итог = сбой, а не «остановлено»."""
    manifest = _batch_manifest(tmp_path, ["Первый", "Второй"])
    codes = iter([1, 130])

    monkeypatch.setattr("gitsync.cli._cmd_sync", lambda args: next(codes))

    code = _cmd_sync_all(argparse.Namespace(config=str(manifest), name=None, log_level="INFO"))

    assert code == 1


# --- D06: справка не привязана к Windows --------------------------------------


def test_v8_path_help_mentions_both_platforms(capsys):
    """Администратор Linux не должен гадать, что подставить вместо ``1cv8.exe``."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sync", "--help"])

    text = capsys.readouterr().out
    assert "1cv8.exe" in text
    assert "1cv8" in text.replace("1cv8.exe", "")


def test_missing_v8_path_error_shows_both_executable_names(tmp_path, capsys):
    code = main(["sync", "--workdir", str(tmp_path / "нет")])

    assert code == 2
    error = capsys.readouterr().err
    assert "1cv8.exe" in error and "/opt/1cv8" in error
