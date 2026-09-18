"""Native15: произвольный UID в приватном образе не должен ронять платформу.

НАЙДЕНО НА ЖИВОЙ ПЛАТФОРМЕ (8.3.27.2130, приватный образ, Docker):

* ``--user 10001:0`` (этот UID есть в ``/etc/passwd``): ``1cv8 CREATEINFOBASE`` → код 0,
  файловая база создана;
* ``--user 12345:0`` (UID не описан в ``/etc/passwd``): тот же запуск →
  ``Segmentation fault (core dumped)``, код 139, базы нет, файл ``/Out`` не создан;
  переменные ``USER``/``LOGNAME`` на это не влияют;
* тот же ``--user 12345:0`` с подложенным ``/etc/passwd``, где строка для 12345 есть →
  снова код 0 и настоящая база.

То есть толстому клиенту нужна разрешимая запись о текущем UID (``getpwuid``), а образ
при этом заявлен как рассчитанный на запуск с произвольным UID. Разрыв закрывается
обёрткой ``docker/onec-user-entrypoint.sh``: если текущего UID нет в ``/etc/passwd``,
она дописывает запись и запускает команду через ``exec``.

Отказ дописать запись НЕ останавливает запуск: команда может вовсе не обращаться к
платформе (``--help``, работа с git), а причину будущего падения оператор увидит в
предупреждении.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "docker" / "onec-user-entrypoint.sh"
DOCKERFILE_NATIVE = ROOT / "docker" / "Dockerfile.native"

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="нужен POSIX sh (в Windows его даёт Git)")
needs_posix = pytest.mark.skipif(
    os.name == "nt",
    reason="сопоставление PID процесса и оболочки достоверно на POSIX; в образе это Linux",
)


def _shell_uid() -> str:
    """UID так, как его видит сама обёртка: у Git-оболочки в Windows он свой."""
    return subprocess.run(
        [SH, "-c", "id -u"], capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()


def _posix(path: Path) -> str:
    text = path.as_posix()
    if os.name == "nt" and len(text) > 2 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def _passwd(tmp_path: Path, *, with_current_uid: bool) -> Path:
    """Файл учётных записей: с записью о текущем UID или заведомо без неё."""
    lines = ["root:x:0:0:root:/root:/bin/bash", "app:x:10001:10001::/home/app:/usr/sbin/nologin"]
    if with_current_uid:
        lines.append(f"current:x:{_shell_uid()}:0::/home/app:/usr/sbin/nologin")
    path = tmp_path / "passwd"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def _env(passwd: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["GITSYNC_PASSWD_FILE"] = _posix(passwd)
    return env


@needs_sh
def test_unknown_uid_gets_passwd_entry(tmp_path):
    """Нет записи о текущем UID — обёртка её дописывает (иначе платформа падает по SIGSEGV)."""
    passwd = _passwd(tmp_path, with_current_uid=False)
    target = _write(tmp_path / "target.sh", "#!/bin/sh\nexit 0\n")

    result = subprocess.run(
        [SH, _posix(WRAPPER), _posix(target)],
        env=_env(passwd), capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    uid = _shell_uid()
    entries = [line.split(":") for line in passwd.read_text(encoding="utf-8").splitlines() if line]
    assert any(parts[2] == uid for parts in entries), (
        f"запись о UID {uid} не добавлена:\n{passwd.read_text(encoding='utf-8')}"
    )


@needs_sh
def test_known_uid_leaves_passwd_untouched(tmp_path):
    """Известный UID — файл учётных записей не трогаем вовсе."""
    passwd = _passwd(tmp_path, with_current_uid=True)
    before = passwd.read_text(encoding="utf-8")
    target = _write(tmp_path / "target.sh", "#!/bin/sh\nexit 0\n")

    result = subprocess.run(
        [SH, _posix(WRAPPER), _posix(target)],
        env=_env(passwd), capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert passwd.read_text(encoding="utf-8") == before


@needs_sh
def test_readonly_passwd_warns_but_runs_command(tmp_path):
    """Дописать некуда — предупреждаем и всё равно запускаем команду."""
    passwd = _passwd(tmp_path, with_current_uid=False)
    marker = tmp_path / "выполнено.txt"
    target = _write(tmp_path / "target.sh", f'#!/bin/sh\ntouch "{_posix(marker)}"\nexit 0\n')
    env = _env(passwd)
    env["GITSYNC_PASSWD_FILE"] = _posix(tmp_path / "нет-такого-файла")

    result = subprocess.run(
        [SH, _posix(WRAPPER), _posix(target)],
        env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists(), "команда не запущена из-за недоступного файла учётных записей"
    assert "passwd" in result.stderr.lower() or "учётн" in result.stderr.lower(), result.stderr


@needs_sh
def test_arguments_pass_through_unchanged(tmp_path):
    """Аргументы команды доходят как есть: пробелы и кириллица не склеиваются."""
    passwd = _passwd(tmp_path, with_current_uid=True)
    out = tmp_path / "args.txt"
    target = _write(
        tmp_path / "target.sh",
        f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a" >> "{_posix(out)}"; done\n',
    )

    result = subprocess.run(
        [SH, _posix(WRAPPER), _posix(target), "sync-all", "--config", "/config/хранилища.json"],
        env=_env(passwd), capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert out.read_text(encoding="utf-8").splitlines() == [
        "sync-all", "--config", "/config/хранилища.json",
    ]


@needs_sh
@needs_posix
def test_wrapper_replaces_itself_with_the_command(tmp_path):
    """Обёртка обязана уступить своё место команде (exec), иначе сигналы до неё не дойдут."""
    passwd = _passwd(tmp_path, with_current_uid=True)
    out = tmp_path / "pid.txt"
    target = _write(tmp_path / "target.sh", f'#!/bin/sh\nprintf %s "$$" > "{_posix(out)}"\n')

    process = subprocess.Popen(
        [SH, _posix(WRAPPER), _posix(target)],
        env=_env(passwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    process.communicate(timeout=60)

    assert out.read_text(encoding="utf-8") == str(process.pid), (
        "команда запущена дочерним процессом: между ней и docker stop остался посредник"
    )


@needs_sh
def test_wrapper_refuses_empty_command(tmp_path):
    passwd = _passwd(tmp_path, with_current_uid=True)

    result = subprocess.run(
        [SH, _posix(WRAPPER)], env=_env(passwd), capture_output=True, text=True, timeout=60,
    )

    assert result.returncode != 0
    assert result.stderr.strip()


def test_native_image_prepares_identity_before_running():
    """Образ обязан ставить обёртку в ENTRYPOINT и разрешать дозапись учётной записи."""
    dockerfile = DOCKERFILE_NATIVE.read_text(encoding="utf-8")

    assert "onec-user-entrypoint" in dockerfile, "обёртка личности не попала в образ"
    entrypoint = dockerfile.split("ENTRYPOINT")[-1]
    assert "onec-user-entrypoint" in entrypoint, "обёртка не включена в ENTRYPOINT"
    assert entrypoint.index("onec-user-entrypoint") < entrypoint.index("xvfb-entrypoint"), (
        "личность должна быть готова до запуска команды"
    )
    assert "/etc/passwd" in dockerfile, (
        "без права дозаписи в /etc/passwd обёртка бессильна при произвольном UID"
    )
