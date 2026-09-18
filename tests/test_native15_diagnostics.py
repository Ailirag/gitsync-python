"""Native15: причина отказа конфигуратора обязана попадать в ошибку.

НАЙДЕНО НА ЖИВОЙ ПЛАТФОРМЕ (8.3.27.2130, Linux, контейнер приватного образа).
При отказе конфигуратор 1С:

* в ``stdout``/``stderr`` пишет постороннее — например ``Fontconfig error: No writable
  cache directories`` в четырёх экземплярах, и больше ничего;
* САМУ ПРИЧИНУ кладёт в файл, переданный ключом ``/Out``:
  «Не найдена лицензия. Не обнаружен ключ защиты программы или полученная программная
  лицензия!»

Из-за этого пакетный прогон по трём настоящим хранилищам выглядел как «три источника
молча вышли с кодом 1»: сообщение продукта состояло из кода возврата и argv, а причина
оставалась в файле, который никто не читал. Здесь это закрывается: текст ``/Out``
обязан попадать в исключение (с вымаранными секретами), а ``CREATEINFOBASE`` обязан
этот файл запрашивать — иначе у самого первого шага диагностики нет вовсе.

Файл ``/Out`` платформа пишет в разных кодировках (UTF-8 с BOM на Linux, UTF-16 и
однобайтовая кодировка Windows — на Windows), поэтому чтение проверяется на всех.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gitsync.designer import DesignerRunner, StorageAccess, read_designer_out
from gitsync.errors import DesignerError

#: Подставной «конфигуратор»: ведёт себя как настоящий — причина ТОЛЬКО в /Out, шум в stderr.
#: Текст причины не передаётся через argv намеренно: иначе проверка прошла бы за счёт
#: эха аргументов в сообщении об ошибке, ничего не проверив.
REASON = "Не найдена лицензия. Не обнаружен ключ защиты программы"

FAKE_CONFIGURATOR = """
import os, sys, pathlib
args = sys.argv[1:]
out = None
password = ""
for index, item in enumerate(args):
    if item == "/Out" and index + 1 < len(args):
        out = args[index + 1]
    if item == "/ConfigurationRepositoryP" and index + 1 < len(args):
        password = args[index + 1]
lines = ["Не найдена лицензия. Не обнаружен ключ защиты программы"]
if password:
    lines.append("повтор аргументов: /ConfigurationRepositoryP " + password)
extra = os.environ.get("GITSYNC_TEST_OUT_EXTRA")
if extra:
    lines.append("повтор значения: " + extra)
if out:
    pathlib.Path(out).write_text("\\ufeff" + "\\n".join(lines) + "\\n", encoding="utf-8")
sys.stderr.write("Fontconfig error: No writable cache directories\\n" * 4)
sys.exit(1)
"""


@pytest.fixture()
def fake_v8(tmp_path) -> Path:
    script = tmp_path / "fake_configurator.py"
    script.write_text(FAKE_CONFIGURATOR, encoding="utf-8")
    return script


def _run_failing(runner: DesignerRunner, fake_v8: Path, extra: list[str]) -> DesignerError:
    with pytest.raises(DesignerError) as excinfo:
        runner.run([sys.executable, str(fake_v8), "DESIGNER", *extra])
    return excinfo.value


def test_error_contains_reason_from_out_file(tmp_path, fake_v8):
    """Причина из файла /Out обязана быть в тексте ошибки, а не только код возврата."""
    runner = DesignerRunner(v8_path=sys.executable, out_dir=tmp_path, timeout=60)
    out = runner.out_file()

    error = _run_failing(runner, fake_v8, ["/Out", str(out)])

    assert REASON in str(error), (
        "продукт обязан показывать причину отказа конфигуратора, а не только argv"
    )
    assert str(out) in str(error), "в сообщении должен быть путь к файлу /Out для разбора"


def test_out_file_reason_shown_before_unrelated_stderr_noise(tmp_path, fake_v8):
    """Шум подсистемы шрифтов не должен оттеснять причину: она идёт первой."""
    runner = DesignerRunner(v8_path=sys.executable, out_dir=tmp_path, timeout=60)
    out = runner.out_file()

    message = str(_run_failing(runner, fake_v8, ["/Out", str(out)]))

    assert message.index(REASON) < message.index("Fontconfig"), (
        "причина обязана стоять раньше постороннего вывода"
    )


def test_secrets_from_out_file_are_masked(tmp_path, fake_v8):
    """Конфигуратор повторяет свои аргументы в /Out — пароль оттуда обязан быть вымаран."""
    runner = DesignerRunner(v8_path=sys.executable, out_dir=tmp_path, timeout=60)
    out = runner.out_file()

    message = str(_run_failing(runner, fake_v8, [
        "/ConfigurationRepositoryP", "секретный-пароль", "/Out", str(out),
    ]))

    assert "повтор аргументов" in message, "текст файла /Out обязан попадать в сообщение"
    assert "секретный-пароль" not in message
    assert "***" in message


def test_registered_secrets_are_masked_in_out_file(tmp_path, fake_v8, monkeypatch):
    """Пароль, зарегистрированный бэкендом (а не переданный в argv), тоже вымарывается."""
    monkeypatch.setenv("GITSYNC_TEST_OUT_EXTRA", "пароль-из-бэкенда")
    runner = DesignerRunner(v8_path=sys.executable, out_dir=tmp_path, timeout=60)
    runner.secrets.append("пароль-из-бэкенда")
    out = runner.out_file()

    message = str(_run_failing(runner, fake_v8, ["/Out", str(out)]))

    assert "повтор значения" in message, "текст файла /Out обязан попадать в сообщение"
    assert "пароль-из-бэкенда" not in message
    assert "***" in message


def test_missing_or_empty_out_file_keeps_previous_behaviour(tmp_path):
    """Без /Out (и при пустом файле) поведение прежнее: показываем вывод процесса."""
    script = tmp_path / "noout.py"
    script.write_text(
        "import sys\nsys.stderr.write('только stderr\\n')\nsys.exit(3)\n", encoding="utf-8"
    )
    runner = DesignerRunner(v8_path=sys.executable, out_dir=tmp_path, timeout=60)

    with pytest.raises(DesignerError) as excinfo:
        runner.run([sys.executable, str(script), "DESIGNER"])

    assert "только stderr" in str(excinfo.value)
    assert "кодом 3" in str(excinfo.value)


@pytest.mark.parametrize(
    ("encoding", "prefix"),
    [
        ("utf-8-sig", ""),
        ("utf-16", ""),
        ("utf-16-le", "﻿"),
        ("cp1251", ""),
        ("utf-8", ""),
    ],
)
def test_out_file_is_read_in_platform_encodings(tmp_path, encoding, prefix):
    """1С пишет /Out в разных кодировках: Linux — UTF-8 с BOM, Windows — UTF-16/однобайтовая."""
    path = tmp_path / f"out-{encoding}.log"
    path.write_text(prefix + "Не найдена лицензия.\n", encoding=encoding)

    assert "Не найдена лицензия." in read_designer_out(path)


def test_out_file_reader_survives_broken_bytes(tmp_path):
    """Битый файл не должен ронять разбор ошибки: диагностика важнее буквальности."""
    path = tmp_path / "broken.log"
    path.write_bytes(b"\xff\xfe\x00\x41\x00")

    read_designer_out(path)  # не должно бросать исключение


def test_out_file_reader_returns_empty_for_absent_file(tmp_path):
    assert read_designer_out(tmp_path / "нет-такого.log") == ""


def test_createinfobase_asks_for_out_file(tmp_path):
    """У CREATEINFOBASE тоже обязан быть /Out: иначе первый же отказ безмолвен."""
    from gitsync.backends import NativeStorageBackend

    calls: list[list[str]] = []

    class _Runner:
        v8_path = "1cv8"

        def __init__(self):
            self.out_dir = tmp_path / "designer-out"
            self.secrets: list[str] = []

        def out_file(self):
            self.out_dir.mkdir(parents=True, exist_ok=True)
            return self.out_dir / "out.log"

        def run(self, args, timeout=None):
            calls.append(list(args))
            spec = next(item for item in args if item.startswith("File="))
            Path(spec[len("File="):]).joinpath("1Cv8.1CD").write_bytes(b"ib")
            return None

    backend = NativeStorageBackend(
        access=StorageAccess(path=str(tmp_path / "repo"), user="acceptance"),
        runner=_Runner(),
        temp_root=tmp_path / "temp",
    )
    backend._create_file_infobase(tmp_path / "worker")

    argv = calls[0]
    assert "/Out" in argv, f"CREATEINFOBASE запущен без /Out: {argv}"
    assert argv[argv.index("/Out") + 1], "после /Out должен стоять путь к файлу"
