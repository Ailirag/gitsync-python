"""Графическое окружение приватного образа не должно ломать остановку контейнера.

Толстый клиент 1С слинкован с GTK3/X11 и без DISPLAY не запускается вовсе
(проверено на 8.3.27.2130: «Unable to initialize GTK+ or connect to the windowing
system»). Поэтому в приватном образе нужен свой X-сервер.

Способ его запуска — НЕ косметика. Штатный `xvfb-run` запускает команду
**дочерним процессом и ждёт её**, чтобы потом убрать за собой X-сервер. Из-за
этого SIGTERM, который `docker stop` посылает процессу №1, до самой синхронизации
не доходит: посредник умирает первым, а работа обрывается на полуслове. В
`docker/compose.yaml` на синхронизацию отведено `stop_grace_period: 70m` — с
таким посредником эта отсрочка не работает вовсе.

Здесь проверяется обёртка `docker/xvfb-entrypoint.sh`: она обязана поднять
X-сервер и **заменить собой** (`exec`) целевую команду, чтобы сигналы приходили
именно ей. `Xvfb` подменяется заглушкой в PATH, поэтому тест не требует ни
контейнера, ни настоящего X-сервера.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "docker" / "xvfb-entrypoint.sh"
DOCKERFILE_NATIVE = ROOT / "docker" / "Dockerfile.native"

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="нужен POSIX sh (в Windows его даёт Git)")
needs_signals = pytest.mark.skipif(
    os.name == "nt",
    reason="доставка SIGTERM процессу проверяется на POSIX; в образе это Linux",
)


def _posix(path: Path) -> str:
    text = path.as_posix()
    if os.name == "nt" and len(text) > 2 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def _xvfb_stub(bin_dir: Path, *, display: str = "99", fail: bool = False) -> None:
    """Заглушка Xvfb: сообщает номер дисплея через -displayfd и живёт до конца теста."""
    if fail:
        body = "#!/bin/sh\necho 'Xvfb: cannot open display' >&2\nexit 1\n"
    else:
        body = (
            "#!/bin/sh\n"
            f"echo {display} >&3\n"
            "while :; do sleep 0.2; done\n"
        )
    _write(bin_dir / "Xvfb", body)


def _env(bin_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env


@needs_sh
def test_wrapper_exports_display_taken_from_xvfb(tmp_path):
    """Целевая команда получает DISPLAY именно того дисплея, который занял Xvfb."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _xvfb_stub(bin_dir, display="77")
    out = tmp_path / "display.txt"
    target = _write(
        tmp_path / "target.sh",
        f'#!/bin/sh\nprintf %s "$DISPLAY" > "{_posix(out)}"\nexit 0\n',
    )

    result = subprocess.run(
        [SH, _posix(WRAPPER), _posix(target)],
        env=_env(bin_dir), capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert out.read_text(encoding="utf-8") == ":77"


@needs_sh
def test_wrapper_passes_arguments_through_unchanged(tmp_path):
    """Аргументы команды не переклеиваются: пробелы и кириллица доходят как есть."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _xvfb_stub(bin_dir)
    out = tmp_path / "args.txt"
    target = _write(
        tmp_path / "target.sh",
        f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a" >> "{_posix(out)}"; done\n',
    )

    result = subprocess.run(
        [SH, _posix(WRAPPER), _posix(target), "sync-all", "--config", "/config/хранилища.json"],
        env=_env(bin_dir), capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert out.read_text(encoding="utf-8").splitlines() == [
        "sync-all", "--config", "/config/хранилища.json",
    ]


@needs_sh
def test_wrapper_stops_when_xvfb_cannot_start(tmp_path):
    """X-сервер не поднялся — команда НЕ запускается, и причина названа.

    Молчаливый запуск без DISPLAY означал бы падение конфигуратора в середине
    работы с невнятным сообщением GTK вместо понятного отказа здесь.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _xvfb_stub(bin_dir, fail=True)
    ran = tmp_path / "ran.txt"
    target = _write(tmp_path / "target.sh", f'#!/bin/sh\ntouch "{_posix(ran)}"\n')

    result = subprocess.run(
        [SH, _posix(WRAPPER), _posix(target)],
        env=_env(bin_dir), capture_output=True, text=True, timeout=120,
    )

    assert result.returncode != 0
    assert not ran.exists(), "команда запущена без работающего X-сервера"
    assert "Xvfb" in result.stderr


@needs_sh
@needs_signals
def test_sigterm_reaches_the_command_itself(tmp_path):
    """SIGTERM процессу обёртки обязан прийти самой команде.

    Это и есть отличие от `xvfb-run`: тот держит команду дочерним процессом,
    забирает сигнал себе и обрывает работу без предупреждения.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _xvfb_stub(bin_dir)
    ready = tmp_path / "ready.txt"
    got_term = tmp_path / "got-term.txt"
    target = _write(
        tmp_path / "target.sh",
        "#!/bin/sh\n"
        f'trap \'touch "{_posix(got_term)}"; exit 0\' TERM\n'
        f'touch "{_posix(ready)}"\n'
        "while :; do sleep 0.1; done\n",
    )

    process = subprocess.Popen(
        [SH, _posix(WRAPPER), _posix(target)],
        env=_env(bin_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            assert process.poll() is None, "обёртка завершилась, не запустив команду"
            time.sleep(0.05)
        assert ready.exists(), "команда не стартовала за отведённое время"

        process.send_signal(signal.SIGTERM)
        returncode = process.wait(timeout=30)
    finally:
        if process.poll() is None:  # pragma: no cover — страховка от зависшего теста
            process.kill()
            process.wait(timeout=30)

    assert got_term.exists(), "SIGTERM не дошёл до самой команды (посредник забрал его себе)"
    assert returncode == 0, f"код возврата принадлежит не команде: {returncode}"


def test_native_image_does_not_interpose_xvfb_run():
    """В ENTRYPOINT приватного образа не должно быть непрозрачного для сигналов посредника."""
    dockerfile = DOCKERFILE_NATIVE.read_text(encoding="utf-8")
    entrypoint = dockerfile.split("ENTRYPOINT")[-1]
    assert "xvfb-run" not in entrypoint, (
        "xvfb-run держит команду дочерним процессом: docker stop не доходит до синхронизации"
    )
    assert "xvfb-entrypoint" in entrypoint
