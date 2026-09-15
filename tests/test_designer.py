"""Слайс 2: контракты вызова нативного конфигуратора 1С.

Аргументы собраны по исходникам ``v8runner`` (см. docs/compatibility-matrix.md). Проверяем,
что команда передаётся массивом argv (без shell), пароли не попадают в логи, а таймаут
и отмена ограничены по времени.
"""

from __future__ import annotations

import pytest

from gitsync.designer import DesignerRunner, StorageAccess, mask_secrets
from gitsync.errors import DesignerTimeoutError


@pytest.fixture()
def runner(tmp_path):
    fake_v8 = tmp_path / "1cv8.exe"
    fake_v8.write_text("", encoding="utf-8")
    return DesignerRunner(v8_path=str(fake_v8), out_dir=tmp_path)


@pytest.fixture()
def access():
    return StorageAccess(path="tcp://srv/хранилище", user="Иванов", password="секрет")


def test_report_args_match_upstream_v8runner(runner, access, tmp_path):
    out = tmp_path / "отчёт.txt"
    args = runner.build_report_args(access, out, begin=5, end=9)

    assert args[0].endswith("1cv8.exe")
    assert args[1] == "DESIGNER"
    assert "/ConfigurationRepositoryF" in args
    assert args[args.index("/ConfigurationRepositoryF") + 1] == "tcp://srv/хранилище"
    assert args[args.index("/ConfigurationRepositoryN") + 1] == "Иванов"
    assert args[args.index("/ConfigurationRepositoryP") + 1] == "секрет"
    assert args[args.index("/ConfigurationRepositoryReport") + 1] == str(out)
    assert args[args.index("-NBegin") + 1] == "5"
    assert args[args.index("-NEnd") + 1] == "9"
    assert "/DisableStartupDialogs" in args and "/DisableStartupMessages" in args


def test_update_cfg_puts_version_flag_after_command(runner, access):
    """Порядок критичен: ``-v`` обязан идти сразу после ConfigurationRepositoryUpdateCfg."""
    args = runner.build_update_cfg_args(access, version=42, ib_connection="/F/tmp/база")

    cmd_index = args.index("/ConfigurationRepositoryUpdateCfg")
    assert args[cmd_index + 1] == "-v"
    assert args[cmd_index + 2] == "42"
    assert "-force" in args[cmd_index:]


def test_dump_args_for_configuration_and_extension(runner):
    plain = runner.build_dump_args("/tmp/каталог выгрузки", ib_connection="/F/tmp/база")
    assert plain[plain.index("/DumpConfigToFiles") + 1] == "/tmp/каталог выгрузки"
    assert "-Extension" not in plain

    ext = runner.build_dump_args("/tmp/вы грузка", ib_connection="/F/tmp/база", extension="МоёРасширение")
    assert ext[ext.index("-Extension") + 1] == "МоёРасширение"

    allext = runner.build_dump_args("/tmp/в", ib_connection="/F/tmp/база", extension="-AllExtensions")
    assert "-AllExtensions" in allext


def test_no_argument_is_shell_quoted_or_concatenated(runner, access, tmp_path):
    """Каждый аргумент — отдельный элемент массива, без кавычек и без склейки со значением."""
    args = runner.build_report_args(access, tmp_path / "о.txt", begin=1)
    assert not [item for item in args if item.startswith('"') or item.endswith('"')]
    assert not [item for item in args if " " in item and item.startswith("/")]


def test_secrets_are_masked_in_logs(runner, access, tmp_path):
    args = runner.build_report_args(access, tmp_path / "о.txt", begin=1)
    masked = mask_secrets(args)

    assert "секрет" not in " ".join(masked)
    assert "***" in " ".join(masked)
    assert "Иванов" in " ".join(masked)


def test_masking_covers_infobase_password_forms():
    masked = mask_secrets(["1cv8", "/P", "pwd", "/PmyPwd", "/ConfigurationRepositoryP", "pwd2"])
    joined = " ".join(masked)
    assert "pwd" not in joined.replace("***", "")
    assert "myPwd" not in joined


def test_run_enforces_timeout_and_reports_masked_command(tmp_path):
    import sys

    runner = DesignerRunner(v8_path=sys.executable, out_dir=tmp_path, timeout=0.5)
    with pytest.raises(DesignerTimeoutError) as excinfo:
        runner.run([sys.executable, "-c", "import time; time.sleep(30)", "/P", "секрет"])

    assert "секрет" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_run_never_uses_shell(tmp_path, monkeypatch):
    import subprocess
    import sys

    captured = {}
    original = subprocess.run

    def spy(*args, **kwargs):
        captured.update(kwargs)
        captured["argv"] = args[0]
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    runner = DesignerRunner(v8_path=sys.executable, out_dir=tmp_path, timeout=30)
    runner.run([sys.executable, "-c", "print(1)"])

    assert captured.get("shell", False) is False
    assert isinstance(captured["argv"], list)
