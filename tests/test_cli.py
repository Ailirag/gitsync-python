"""Слайс 4: CLI и пакетная обработка нескольких хранилищ на файловом бэкенде-фикстуре."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from gitsync.cli import main

REPORT = """Отчет по версиям хранилища конфигурации

Версия:                  1
Пользователь:            Иванов
Дата создания:           15.09.2026 10:20:30
Комментарий:             Первая версия

Версия:                  2
Пользователь:            Петров
Дата создания:           16.09.2026 08:00:00
Комментарий:             Вторая версия
"""


@pytest.fixture()
def fixture_storage(tmp_path):
    root = tmp_path / "фикстура хранилища"
    root.mkdir()
    (root / "report.txt").write_text(REPORT, encoding="utf-8")
    (root / "v1" / "Справочники").mkdir(parents=True)
    (root / "v1" / "Справочники" / "Товары.xml").write_text("<Товары v='1'/>", encoding="utf-8")
    (root / "v2" / "Справочники").mkdir(parents=True)
    (root / "v2" / "Справочники" / "Товары.xml").write_text("<Товары v='2'/>", encoding="utf-8")
    return root


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


def test_cli_sync_end_to_end_with_fixture_backend(tmp_path, fixture_storage):
    work = tmp_path / "рабочая копия"

    code = main(
        [
            "sync",
            "--workdir", str(work),
            "--backend", "fixture",
            "--fixture-root", str(fixture_storage),
            "--jobs", "2",
            "--email-domain", "example.org",
        ]
    )

    assert code == 0
    subjects = [line for line in _git(work, "log", "--format=%s", "--reverse").split("\n") if line]
    assert subjects == ["Первая версия", "Вторая версия"]
    assert "<VERSION>2</VERSION>" in (work / "VERSION").read_text(encoding="utf-8")
    assert (work / "Справочники" / "Товары.xml").read_text(encoding="utf-8") == "<Товары v='2'/>"
    assert _git(work, "log", "-1", "--format=%an|%ae").strip() == "Петров|Петров@example.org"


def test_cli_init_creates_authors_and_version(tmp_path, fixture_storage):
    work = tmp_path / "новая копия"

    assert main(["init", "--workdir", str(work), "--backend", "fixture",
                 "--fixture-root", str(fixture_storage), "--email-domain", "corp.local"]) == 0

    authors = (work / "AUTHORS").read_text(encoding="utf-8")
    assert "Иванов=Иванов <Иванов@corp.local>" in authors
    assert "Петров=Петров <Петров@corp.local>" in authors
    assert (work / "VERSION").is_file()
    assert (work / ".git").is_dir()


def test_cli_set_version_writes_marker(tmp_path):
    work = tmp_path / "копия"
    work.mkdir()
    assert main(["set-version", "--workdir", str(work), "--version", "77"]) == 0
    assert "<VERSION>77</VERSION>" in (work / "VERSION").read_text(encoding="utf-8")


def test_cli_sync_all_processes_several_storages(tmp_path, fixture_storage):
    config = {
        "defaults": {"backend": "fixture", "jobs": 2, "email_domain": "example.org"},
        "storages": [
            {"name": "первое", "workdir": str(tmp_path / "wc1"), "fixture_root": str(fixture_storage)},
            {"name": "второе", "workdir": str(tmp_path / "wc2"), "fixture_root": str(fixture_storage)},
        ],
    }
    config_path = tmp_path / "хранилища.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    assert main(["sync-all", "--config", str(config_path)]) == 0

    for name in ("wc1", "wc2"):
        assert "<VERSION>2</VERSION>" in (tmp_path / name / "VERSION").read_text(encoding="utf-8")


def test_cli_sync_all_reports_failure_but_continues_other_storages(tmp_path, fixture_storage):
    config = {
        "defaults": {"backend": "fixture", "jobs": 1},
        "storages": [
            {"name": "битое", "workdir": str(tmp_path / "bad"), "fixture_root": str(tmp_path / "нет")},
            {"name": "живое", "workdir": str(tmp_path / "ok"), "fixture_root": str(fixture_storage)},
        ],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    code = main(["sync-all", "--config", str(config_path)])

    assert code != 0
    assert "<VERSION>2</VERSION>" in (tmp_path / "ok" / "VERSION").read_text(encoding="utf-8")


def test_cli_password_is_taken_from_env_not_argv(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITSYNC_TEST_PASSWORD", "очень секретный")
    from gitsync.cli import build_storage_access

    access = build_storage_access(
        storage_path="tcp://srv/x", user="Иванов", password_env="GITSYNC_TEST_PASSWORD",
        password_file=None,
    )

    assert access.password == "очень секретный"


def test_cli_rejects_password_on_command_line():
    with pytest.raises(SystemExit):
        main(["sync", "--workdir", ".", "--storage-password", "секрет"])


def test_cli_help_is_russian_and_lists_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "хранилищ" in out
    for command in ("init", "clone", "sync", "set-version", "sync-all"):
        assert command in out


def test_module_entry_point_runs(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "gitsync", "--version"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "gitsync-py" in result.stdout


def test_example_fixture_storage_is_usable(tmp_path):
    """Пример из examples/ должен реально работать — иначе он вводит в заблуждение."""
    from pathlib import Path

    example_root = Path(__file__).resolve().parent.parent / "examples" / "fixture-storage"
    work = tmp_path / "пример"

    assert main(["sync", "--workdir", str(work), "--backend", "fixture",
                 "--fixture-root", str(example_root), "--jobs", "2"]) == 0

    first = _git(work, "log", "--format=%B", "--reverse", "-1", "HEAD~1").strip()
    head = _git(work, "log", "--format=%B", "-1", "HEAD").strip()
    assert first == "Первая версия конфигурации"
    # Многострочный комментарий из отчёта сохраняется в теле коммита как есть.
    assert head == "Доработка справочника Товары\nвторая строка комментария"
    # Объект, которого нет во второй версии, должен исчезнуть из рабочей копии.
    assert not (work / "Справочники" / "Контрагенты.xml").exists()
