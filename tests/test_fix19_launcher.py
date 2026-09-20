"""Очередь допуска контейнерных заданий: examples/gitsync-run.sh.

Платформа 1С удерживает лицензию на время работы конфигуратора, и два одновременных
задания приводили к отказу «Не найдена лицензия» у одного из них. Очередь ничего не
меняет в лицензионном механизме — она сериализует ЗАПУСКИ. Проверяется ровно это и
свойства самого замка: чужой файл не удаляется, ожидание ограничено, код возврата
задания проходит насквозь, небезопасный каталог очереди — отказ, а не «и так сойдёт».

Запуски здесь синтетические: вместо ``docker`` в ``PATH`` подставлен сценарий, который
записывает время своей работы. Настоящий контейнерный прогон — отдельная приёмка.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "examples" / "gitsync-run.sh"
ENV_EXAMPLE = ROOT / "examples" / "gitsync-run.env.example"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("flock") is None,
    reason="очередь построена на flock: сценарий для POSIX-хоста",
)


@pytest.fixture()
def stand(tmp_path):
    """Каталог очереди, поддельный docker в PATH и готовое окружение запуска."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    journal = tmp_path / "journal.txt"
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/bin/bash\n"
        "# Поддельный docker: пишет свои границы работы и возвращает заданный код.\n"
        'if [ "$1" = "stop" ] || [ "$1" = "rm" ]; then exit 0; fi\n'
        'name=""; rc=0; hold="${FAKE_HOLD:-0.6}"\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in --name) name=$2; shift 2;; --rc) rc=$2; shift 2;; *) shift;; esac\n'
        "done\n"
        f'note() {{ echo "{{\\"name\\": \\"$name\\", \\"phase\\": \\"$1\\", '
        f'\\"at\\": $(date +%s.%N)}}" >> "{journal}"; }}\n'
        'note start\n'
        'sleep "$hold"\n'
        'note end\n'
        'exit "$rc"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    environment["GITSYNC_IMAGE"] = "sha256:test"
    environment["GITSYNC_ADMISSION_DIR"] = str(tmp_path / "admission")
    environment["GITSYNC_ADMISSION_WAIT"] = "30"
    return type("Stand", (), {"env": environment, "journal": journal, "root": tmp_path})()


def _run(stand, *args, timeout=120, **overrides):
    environment = {**stand.env, **overrides}
    return subprocess.run(["bash", str(LAUNCHER), *args], capture_output=True, text=True,
                          env=environment, timeout=timeout, check=False)


def _intervals(journal: Path) -> dict[str, tuple[float, float]]:
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        (starts if record["phase"] == "start" else ends)[record["name"]] = float(record["at"])
    return {name: (starts[name], ends[name]) for name in starts if name in ends}


def test_two_simultaneous_submissions_run_one_after_another(stand):
    """Два задания, поданных одновременно, выполняются последовательно и оба успешно."""
    first = subprocess.Popen(["bash", str(LAUNCHER), "sync", "--jobs", "1"],
                             env={**stand.env, "GITSYNC_NAME_PREFIX": "job-a"},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(["bash", str(LAUNCHER), "sync", "--jobs", "1"],
                              env={**stand.env, "GITSYNC_NAME_PREFIX": "job-b"},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first.communicate(timeout=180)
    second.communicate(timeout=180)

    assert first.returncode == 0 and second.returncode == 0
    intervals = _intervals(stand.journal)
    assert len(intervals) == 2, f"оба задания должны были выполниться: {intervals}"
    (first_start, first_end), (second_start, second_end) = sorted(intervals.values())
    assert first_end <= second_start, (
        f"работа заданий перекрылась: {intervals} — очередь не сериализовала запуск"
    )


def test_container_exit_code_passes_through(stand):
    result = _run(stand, "--rc", "3")
    assert result.returncode == 3, result.stderr


def test_busy_queue_ends_with_a_clear_code_and_keeps_the_lock_file(stand):
    """Занятая очередь — код 75 и понятное сообщение, а чужой замок остаётся на месте."""
    admission = Path(stand.env["GITSYNC_ADMISSION_DIR"])
    admission.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = admission / "slot-1.lock"
    lock.touch(mode=0o600)
    holder = subprocess.Popen(["flock", "-x", str(lock), "-c", "sleep 30"])
    try:
        time.sleep(0.5)
        result = _run(stand, "sync", GITSYNC_ADMISSION_WAIT="1")
        assert result.returncode == 75, (result.returncode, result.stderr)
        assert "очередь занята" in result.stderr
        assert lock.exists(), "чужой файл замка снесён: координация сломана"
        assert not stand.journal.exists(), "задание не должно было запускаться"
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_world_writable_admission_directory_is_refused(stand):
    admission = Path(stand.env["GITSYNC_ADMISSION_DIR"])
    admission.mkdir(mode=0o777, parents=True)
    os.chmod(admission, 0o777)
    result = _run(stand, "sync")
    assert result.returncode == 77, (result.returncode, result.stderr)
    assert "может писать не только владелец" in result.stderr


def test_missing_image_is_a_usage_error(stand):
    result = _run(stand, "sync", GITSYNC_IMAGE="")
    assert result.returncode == 64
    assert "GITSYNC_IMAGE" in result.stderr


def test_launcher_never_removes_lock_files():
    """Удаление файла, на котором висит чужой flock, снимает координацию целиком."""
    import re

    text = LAUNCHER.read_text(encoding="utf-8")
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        # Команда удаления в позиции команды: `docker rm` и ключ `--rm` сюда не попадают.
        found = re.search(r"(?:^|[;&|(]\s*)(rm|unlink|shred)\b", code)
        assert not found, f"замки удалять нельзя, а в сценарии есть удаление: {line}"
    assert 'docker run --rm --name "$NAME"' in text, "контейнер обязан быть одноразовым и именованным"
    assert "trap 'on_signal INT' INT" in text and "trap 'on_signal TERM' TERM" in text


def test_example_environment_has_no_secrets():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    lowered = text.lower()
    for forbidden in ("password=", "passwd=", "token=", "secret=", "apikey="):
        assert forbidden not in lowered, f"в примере окружения не место секретам: {forbidden}"
    assert "GITSYNC_SLOTS=1" in text, "умолчание обязано быть подтверждённым значением"
