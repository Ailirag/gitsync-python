"""Предполётная проверка контейнера исполняется, а не пересказывается.

``docker/entrypoint.sh`` — обычный POSIX sh, поэтому он запускается и на машине
разработки: ``git``, ``gitsync-py`` и ``id`` подменяются заглушками в PATH, а
проверяемые условия задаются переменными окружения и правами на каталоги.

Граница обозначена явно: настоящие тома, bind-монтирование и чужой владелец
каталога проверяются только запуском в контейнере (см. ``docs/docker-guide.md``);
здесь проверяется логика самой предполётной проверки — какие случаи она обязана
остановить и что обязана назвать в сообщении.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="нужен POSIX sh (в Windows его даёт Git)")


def _posix(path: Path) -> str:
    """Путь в том виде, в каком его понимает POSIX-оболочка (в Windows — `/c/...`)."""
    text = path.as_posix()
    if os.name == "nt" and len(text) > 2 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


def _stubs(tmp_path: Path, *, uid: str = "10001", gid: str = "0") -> Path:
    """Каталог с заглушками программ, которые ищет предполётная проверка."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "gitsync-py").write_text(
        "#!/bin/sh\necho \"GITSYNC-STUB $*\"\nexit 0\n", encoding="utf-8", newline="\n")
    (bin_dir / "git").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    # `id -un` у UID без записи в /etc/passwd завершается ошибкой — именно это и
    # проверяет предполётная проверка, поэтому заглушка умеет обе роли.
    (bin_dir / "id").write_text(
        "#!/bin/sh\n"
        f'case "$1" in\n'
        f'  -u) echo {uid} ;;\n'
        f'  -g) echo {gid} ;;\n'
        f'  -un) if [ "${{STUB_PASSWD_ENTRY:-1}}" = "1" ]; then echo пользователь; else\n'
        f'         echo "id: cannot find name for user ID {uid}" >&2; exit 1; fi ;;\n'
        f'  *) echo {uid} ;;\n'
        f'esac\n',
        encoding="utf-8", newline="\n")
    for name in ("gitsync-py", "git", "id"):
        (bin_dir / name).chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, *args: str, uid: str = "10001", gid: str = "0",
         **env: str) -> subprocess.CompletedProcess[str]:
    bin_dir = _stubs(tmp_path, uid=uid, gid=gid)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    окружение = dict(os.environ)
    окружение.pop("GITSYNC_SESSION_DIR", None)
    окружение.pop("TZ", None)
    окружение["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    окружение["HOME"] = _posix(home)
    for key, value in env.items():
        if value is None:
            окружение.pop(key, None)
        else:
            окружение[key] = value
    return subprocess.run([SH, str(ENTRYPOINT), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=окружение,
                          check=False, timeout=120)


@needs_sh
def test_configured_container_runs_the_tool(tmp_path):
    result = _run(tmp_path, "--help")
    assert result.returncode == 0, result.stderr
    assert "GITSYNC-STUB --help" in result.stdout


@needs_sh
def test_root_is_refused_before_any_work(tmp_path):
    """Образ рассчитан на обычного пользователя: root — отказ, а не предупреждение."""
    result = _run(tmp_path, "--help", uid="0", gid="0")
    assert result.returncode == 78, result.stdout + result.stderr
    assert "GITSYNC-STUB" not in result.stdout, "работа началась несмотря на отказ"
    assert "root" in result.stderr
    assert "GITSYNC_ALLOW_ROOT" in result.stderr, "в сообщении должен быть явный способ согласиться"


@needs_sh
def test_root_requires_an_explicit_opt_in(tmp_path):
    result = _run(tmp_path, "--help", uid="0", gid="0", GITSYNC_ALLOW_ROOT="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GITSYNC-STUB --help" in result.stdout
    assert "root" in result.stderr, "согласие не отменяет предупреждения"


@needs_sh
def test_unwritable_home_names_the_supported_way_to_run(tmp_path):
    """Штатный способ запуска назван прямо в сообщении, а не только в руководстве."""
    занято = tmp_path / "занято"
    занято.write_text("это файл, а не каталог", encoding="utf-8")
    result = _run(tmp_path, "--help", HOME=_posix(занято / "home"))

    assert result.returncode == 78, result.stdout + result.stderr
    # Каталоги образа принадлежат app:0 с правами группы, поэтому рабочий способ —
    # свой UID и GID 0; второй способ — пересборка образа под свой UID.
    assert ":0" in result.stderr
    assert "APP_UID" in result.stderr


@needs_sh
def test_uid_without_a_passwd_entry_is_named_before_ssh_fails(tmp_path):
    """UID без записи в /etc/passwd работает, но ssh откажет — об этом надо сказать сразу.

    Проверено на стенде: `git push` по ssh из контейнера с `--user 1000:0` падает с
    «No user exists for uid 1000» — OpenSSH спрашивает getpwuid до всякой сети.
    Синхронизации это не мешает, поэтому здесь предупреждение, а не отказ.
    """
    result = _run(tmp_path, "--help", uid="1000", gid="0", STUB_PASSWD_ENTRY="0")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GITSYNC-STUB --help" in result.stdout
    assert "/etc/passwd" in result.stderr
    assert "ssh" in result.stderr
    assert "APP_UID" in result.stderr, "надо назвать способ это исправить"


@needs_sh
def test_known_uid_is_not_warned_about(tmp_path):
    result = _run(tmp_path, "--help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "/etc/passwd" not in result.stderr


@needs_sh
def test_session_directory_must_be_absolute(tmp_path):
    result = _run(tmp_path, "--help", GITSYNC_SESSION_DIR="relative/sessions")
    assert result.returncode == 78, result.stdout + result.stderr
    assert "абсолютным" in result.stderr


@needs_sh
def test_unusable_session_directory_names_the_volume_commands(tmp_path):
    занято = tmp_path / "файл-вместо-тома"
    занято.write_text("не каталог", encoding="utf-8")
    result = _run(tmp_path, "--help", GITSYNC_SESSION_DIR=_posix(занято / "sessions"))

    assert result.returncode == 78, result.stdout + result.stderr
    assert "docker volume create" in result.stderr


@needs_sh
def test_sync_without_timezone_stops_before_work(tmp_path):
    result = _run(tmp_path, "sync-all", "--config", "/config/storages.json")
    assert result.returncode == 78, result.stdout + result.stderr
    assert "TZ" in result.stderr


@needs_sh
def test_timezone_is_not_required_for_other_commands(tmp_path):
    result = _run(tmp_path, "--version")
    assert result.returncode == 0, result.stdout + result.stderr
