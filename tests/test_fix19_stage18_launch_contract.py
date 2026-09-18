"""Договор о каталоге запуска и владение контейнером: examples/gitsync-run.sh (stage-18).

ПОЧЕМУ ЭТИ ПРОВЕРКИ ЕСТЬ. Каталог запуска (scratch) продукт по умолчанию кладёт рядом с
рабочей копией. Значит его место определяют монтирования, и это измерено на стенде
(evidence/stage-18/10-isolation-probe.log, 11-scratch-contract-probe.log):

* смонтирован весь ``/work`` — scratch лежит на bind-mount с хоста, и ДРУГОЙ штатный
  контейнер с тем же UID 10001 видит выгрузку, читает её и уничтожает ``rm -rf``;
* смонтирована только рабочая копия (``/work/repo``) — scratch остаётся на собственном
  слое контейнера (overlay), и тот же нападающий не видит каталога вовсе.

Поэтому первый режим не поддерживается: сценарий обязан ОТКАЗАТЬ до старта контейнера,
то есть до любого удаления. Здесь проверяется ровно это, а также то, что сигнал никогда
не останавливает чужой контейнер: свой ищется по уникальной метке запуска, а не по имени.

Границей остаётся враждебный код под тем же UID ВНУТРИ того же контейнера — она измерена
в stage-17 и здесь не закрывается и не заявляется закрытой.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "examples" / "gitsync-run.sh"

EX_CONTRACT = 78

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("flock") is None,
    reason="очередь и договор построены на flock и docker: сценарий для POSIX-хоста",
)


@pytest.fixture()
def stand(tmp_path):
    """Поддельный docker, который записывает КАЖДЫЙ свой вызов.

    Журнал вызовов — единственный способ доказать, что остановки чужого контейнера не
    было: отсутствие команды доказывается только полным списком выполненных команд.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    journal = tmp_path / "journal.txt"
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'case "$1" in\n'
        # `docker ps` отдаёт то, что задано стендом: пусто = своего контейнера нет.
        '  ps) [ -n "${FAKE_PS_ID:-}" ] && echo "$FAKE_PS_ID"; exit 0;;\n'
        '  stop|rm) exit 0;;\n'
        "esac\n"
        'rc=0; hold="${FAKE_HOLD:-0.4}"\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in --rc) rc=$2; shift 2;; *) shift;; esac\n'
        "done\n"
        f'echo ran >> "{journal}"\n'
        'sleep "$hold"\n'
        'exit "$rc"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    environment["GITSYNC_IMAGE"] = "sha256:test"
    environment["GITSYNC_ADMISSION_DIR"] = str(tmp_path / "admission")
    environment["GITSYNC_ADMISSION_WAIT"] = "30"
    return type("Stand", (), {
        "env": environment, "calls": calls, "journal": journal, "root": tmp_path,
    })()


def _run(stand, *args, timeout=120, **overrides):
    environment = {**stand.env, **overrides}
    return subprocess.run(["bash", str(LAUNCHER), *args], capture_output=True, text=True,
                          env=environment, timeout=timeout, check=False)


def _calls(stand) -> list[str]:
    if not stand.calls.exists():
        return []
    return [line for line in stand.calls.read_text(encoding="utf-8").splitlines() if line]


# --- договор о каталоге запуска ----------------------------------------------


SCRATCH = "/var/lib/gitsync/scratch"


@pytest.mark.parametrize("docker_args", [
    f"-v /srv/data:{SCRATCH}",
    f"-v /srv/data:{SCRATCH}/",
    f"--volume /srv/data:{SCRATCH}",
    f"--volume=/srv/data:{SCRATCH}",
    f"--mount type=bind,src=/srv/data,dst={SCRATCH}",
    f"--mount type=bind,source=/srv/data,target={SCRATCH}",
    f"--tmpfs {SCRATCH}",
    "-v scratchvol:/var/lib/gitsync",           # том docker замерен как достижимый
    f"-v /srv/data:{SCRATCH}/versions",          # монтирование ВНУТРЬ каталога запуска
    "-v /srv/data:/",
    f"-v /srv/repo:/work/repo -v /srv/data:{SCRATCH}",
])
def test_a_mount_covering_the_scratch_is_refused_before_the_container_starts(stand, docker_args):
    """Уязвимый режим не «принимается с оговоркой», а отвергается ДО запуска."""
    result = _run(stand, "sync", GITSYNC_DOCKER_ARGS=docker_args)
    assert result.returncode == EX_CONTRACT, (result.returncode, result.stderr)
    assert "каталог запуска" in result.stderr
    assert not stand.journal.exists(), "задание не должно было запускаться"
    assert not any(line.startswith("run ") for line in _calls(stand)), \
        f"контейнер не должен был стартовать: {_calls(stand)}"


@pytest.mark.parametrize("docker_args", [
    "-v /srv/repo:/work/repo",
    "-v /srv/repo:/work/repo:ro -v /srv/storages:/storages",
    "--mount type=bind,src=/srv/repo,dst=/repo",
    "-v /srv/sessions:/var/lib/gitsync/sessions",   # сосед каталога запуска, не он сам
    "-v /srv/work:/work",
    "",
])
def test_a_mount_beside_the_scratch_is_allowed(stand, docker_args):
    """Рабочая копия, хранилища и том блокировок монтируются — это штатный режим."""
    result = _run(stand, "sync", GITSYNC_DOCKER_ARGS=docker_args)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert stand.journal.exists(), "штатный запуск обязан был состояться"


def test_the_container_is_told_the_same_scratch_that_was_checked(stand):
    """Проверенный путь и применённый обязаны совпадать, иначе проверка ничего не значит."""
    result = _run(stand, "sync")
    assert result.returncode == 0, result.stderr
    started = [line for line in _calls(stand) if "--label" in line]
    assert started, _calls(stand)
    assert f"-e GITSYNC_SCRATCH={SCRATCH}" in started[0], started[0]
    assert "-e GITSYNC_REQUIRE_PRIVATE_SCRATCH=1" in started[0], started[0]


def test_the_scratch_is_raised_as_tmpfs_because_overlay_breaks_parallel_export(stand):
    """Каталог запуска поднимается как tmpfs — это ЗАМЕР, а не предпочтение.

    При jobs=2 конфигуратор на слое контейнера (overlay) отвечает «Не найдена лицензия»
    и доводит 13-14 версий из 17; на tmpfs — 17 из 17 (evidence/stage-18: 42, 44, 46, 47).
    Изоляция у обоих вариантов одинаковая, работоспособность — нет.
    """
    result = _run(stand, "sync")
    assert result.returncode == 0, result.stderr
    started = [line for line in _calls(stand) if "--label" in line]
    assert started, _calls(stand)
    assert f"--tmpfs {SCRATCH}:rw,mode=1777,size=2g" in started[0], started[0]


def test_the_tmpfs_size_is_operator_settable(stand):
    result = _run(stand, "sync", GITSYNC_SCRATCH_TMPFS="8g")
    assert result.returncode == 0, result.stderr
    started = [line for line in _calls(stand) if "--label" in line]
    assert "size=8g" in started[0], started[0]


def test_turning_the_tmpfs_off_is_possible_and_says_what_it_costs(stand):
    """Отключение возможно, но молча не проходит: цена названа в тот же момент."""
    result = _run(stand, "sync", GITSYNC_SCRATCH_TMPFS="off")
    assert result.returncode == 0, result.stderr
    started = [line for line in _calls(stand) if "--label" in line]
    assert "--tmpfs" not in started[0], started[0]
    assert "jobs>1" in result.stderr, result.stderr


def test_temp_root_pointing_into_a_mounted_volume_is_refused(stand):
    """--temp-root переносит каталог запуска: проверяется он, а не умолчание.

    Это ровно тот режим, который был записан в руководстве: temp_root на /work/tmp,
    смонтированном томом.
    """
    result = _run(stand, "sync", "--temp-root", "/work/tmp",
                  GITSYNC_DOCKER_ARGS="-v worktmp:/work/tmp")
    assert result.returncode == EX_CONTRACT, (result.returncode, result.stderr)
    assert "/work/tmp" in result.stderr


def test_temp_root_in_equals_form_is_refused_too(stand):
    result = _run(stand, "sync", "--temp-root=/work/tmp",
                  GITSYNC_DOCKER_ARGS="-v worktmp:/work/tmp")
    assert result.returncode == EX_CONTRACT, (result.returncode, result.stderr)


def test_temp_root_outside_every_mount_is_allowed(stand):
    result = _run(stand, "sync", "--temp-root", "/var/tmp/own-scratch",
                  GITSYNC_DOCKER_ARGS="-v /srv/repo:/work/repo")
    assert result.returncode == 0, (result.returncode, result.stderr)


def test_relative_temp_root_is_refused(stand):
    """Относительный путь неразрешим снаружи контейнера — проверить его нельзя."""
    result = _run(stand, "sync", "--temp-root", "tmp",
                  GITSYNC_DOCKER_ARGS="-v /srv/repo:/work/repo")
    assert result.returncode == EX_CONTRACT, (result.returncode, result.stderr)
    assert "абсолютным" in result.stderr


def test_the_scratch_path_itself_can_be_moved_by_the_operator(stand):
    """GITSYNC_SCRATCH объявляет фактический каталог запуска — проверяется объявленный."""
    refused = _run(stand, "sync", GITSYNC_SCRATCH="/opt/scratch",
                   GITSYNC_DOCKER_ARGS="-v /srv/data:/opt/scratch")
    assert refused.returncode == EX_CONTRACT, (refused.returncode, refused.stderr)
    allowed = _run(stand, "sync", GITSYNC_SCRATCH="/opt/scratch",
                   GITSYNC_DOCKER_ARGS=f"-v /srv/data:{SCRATCH}")
    assert allowed.returncode == 0, (allowed.returncode, allowed.stderr)
    started = [line for line in _calls(stand) if "--label" in line]
    assert all("-e GITSYNC_SCRATCH=/opt/scratch" in line for line in started), started


# --- владение контейнером ----------------------------------------------------


def test_signal_never_stops_a_container_that_is_not_ours(stand):
    """Своего контейнера нет — не выполняется НИ ОДНОЙ команды остановки.

    Это и есть суть замечания: `docker stop <имя>` по предсказуемому имени останавливает
    чужую работу. Пустой ответ `docker ps` по нашей метке означает «нашего контейнера
    здесь нет», и правильное поведение — не трогать ничего.
    """
    process = subprocess.Popen(["bash", str(LAUNCHER), "sync"],
                               env={**stand.env, "FAKE_HOLD": "30", "FAKE_PS_ID": ""},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(200):
            if stand.journal.exists():
                break
            time.sleep(0.05)
        assert stand.journal.exists(), "поддельный контейнер не стартовал"
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)

    assert process.returncode == 143, process.returncode
    stopped = [line for line in _calls(stand) if line.startswith(("stop", "rm"))]
    assert stopped == [], f"остановлено то, что нам не принадлежит: {stopped}"


def test_signal_stops_our_container_by_id_and_never_by_name(stand):
    """Свой контейнер останавливается по идентификатору из docker ps по нашей метке."""
    fake_id = "c0ffee1234567890c0ffee1234567890c0ffee1234567890c0ffee1234567890"
    process = subprocess.Popen(["bash", str(LAUNCHER), "sync"],
                               env={**stand.env, "FAKE_HOLD": "30", "FAKE_PS_ID": fake_id,
                                    "GITSYNC_NAME_PREFIX": "job-owned"},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(200):
            if stand.journal.exists():
                break
            time.sleep(0.05)
        assert stand.journal.exists(), "поддельный контейнер не стартовал"
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)

    calls = _calls(stand)
    stopped = [line for line in calls if line.startswith("stop")]
    assert stopped, f"свой контейнер обязан быть остановлен: {calls}"
    for line in stopped:
        assert fake_id in line, f"остановка не по идентификатору: {line}"
        assert "job-owned" not in line, f"остановка по имени — так можно снести чужое: {line}"
    lookups = [line for line in calls if line.startswith("ps ")]
    assert lookups, "свой контейнер обязан искаться явным запросом"
    for line in lookups:
        assert "label=com.gitsync.job=" in line, f"поиск не по своей метке: {line}"


def test_each_run_gets_its_own_job_label(stand):
    """Метка запуска уникальна: иначе по ней нашёлся бы контейнер соседнего задания."""
    first = _run(stand, "sync")
    second = _run(stand, "sync")
    assert first.returncode == 0 and second.returncode == 0
    tags = re.findall(r"label com\.gitsync\.job=(\S+)", "\n".join(_calls(stand)))
    assert len(tags) == 2, f"обе метки должны быть в журнале вызовов: {_calls(stand)}"
    assert tags[0] != tags[1], "метка повторилась: задания перестали различаться"
    for tag in tags:
        assert len(tag) >= 16, f"метка слишком коротка, чтобы быть уникальной: {tag}"


def test_the_launcher_has_unix_line_endings():
    """CRLF в этом файле — не косметика: оболочка внутри образа на нём падает.

    Так и случилось в stage-18: правка через Python на Windows перевела файл в CRLF,
    и запуск в контейнере завершился `syntax error near unexpected token $'{\\r'`.
    Проверка стоит здесь, чтобы это не повторилось молча.
    """
    raw = LAUNCHER.read_bytes()
    assert b"\r\n" not in raw, "в пусковом сценарии CRLF: оболочка в образе на нём упадёт"


def test_launcher_documents_the_measured_contract():
    """Договор обязан быть записан там же, где исполняется, — иначе его не соблюдут."""
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "com.gitsync.job=" in text, "контейнер обязан помечаться меткой запуска"
    assert 'docker stop -t 30 "$NAME"' not in text, "остановка по имени снимает чужой контейнер"
    assert 'docker rm -f "$NAME"' not in text, "удаление по имени снимает чужой контейнер"
    assert "EX_CONTRACT=78" in text
