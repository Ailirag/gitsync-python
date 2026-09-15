"""Эксклюзивная блокировка целевого репозитория.

Блокировка межпроцессная и держится **блокировкой ОС на открытом дескрипторе**
(``msvcrt.locking`` в Windows, ``fcntl.flock`` в POSIX), а не фактом существования файла.
Это принципиально: при аварийном завершении владельца (kill, паника, обрыв питания процесса)
ядро освобождает блокировку само, поэтому следующий запуск восстанавливается штатно и
не требует ни ручного удаления файла, ни «лечения» по ненадёжному PID — удалить файл по чужому
PID значило бы отобрать блокировку у живого владельца при переиспользовании идентификатора.

Файл остаётся на диске после освобождения (он же носитель блокировки) и содержит pid/хост
владельца — только для диагностики. Содержимое чужого файла в сообщение не подставляется
дословно: путь может оказаться подсунутой символьной ссылкой на посторонний файл.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import time
from collections.abc import Iterator
from pathlib import Path

from .errors import LockBusyError, UnsafePathError

try:  # pragma: no cover — ветка выбирается платформой
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover — ветка выбирается платформой
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]

#: В диагностику попадает только то, что мы сами пишем: pid + имя хоста.
_HOLDER_RE = re.compile(r"^pid=(\d{1,10}) host=([\w.\-]{1,64})$")


def _try_lock(fd: int) -> bool:
    """Пытается взять блокировку ОС без ожидания. ``False`` — держит кто-то другой."""
    if msvcrt is not None:  # Windows
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    if fcntl is not None:  # POSIX
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    raise RuntimeError("Платформа не поддерживает блокировку файлов")  # pragma: no cover


def _unlock(fd: int) -> None:
    if msvcrt is not None:  # pragma: no branch — одна ветка на платформу
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    if fcntl is not None:  # pragma: no cover
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive_lock(path: str | Path, timeout: float = 30.0, poll: float = 0.05) -> Iterator[Path]:
    """Держит эксклюзивную блокировку ``path`` или падает с :class:`LockBusyError`."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise UnsafePathError(f"Файл блокировки <{lock_path}> — символьная ссылка, это небезопасно")
    deadline = time.monotonic() + max(timeout, 0.0)

    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_NOFOLLOW", 0)  # POSIX: не идти по символьной ссылке
    fd = os.open(str(lock_path), flags, 0o600)
    try:
        while True:
            if _try_lock(fd):
                break
            if time.monotonic() >= deadline:
                raise LockBusyError(
                    f"Цель <{lock_path}> уже обрабатывается{_read_holder(lock_path)}; "
                    f"ожидание {timeout:g} с истекло"
                )
            time.sleep(poll)

        try:
            os.truncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"pid={os.getpid()} host={socket.gethostname()}\n".encode())
            # Дескриптор остаётся под блокировкой ОС: позиция возвращается на байт 0,
            # который и заблокирован (важно для msvcrt — блокируется диапазон от позиции).
            os.lseek(fd, 0, os.SEEK_SET)
            yield lock_path
        finally:
            _unlock(fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _read_holder(lock_path: Path) -> str:
    """Диагностика владельца. Произвольный текст наружу не отдаётся."""
    try:
        with open(lock_path, encoding="utf-8", errors="replace") as handle:
            text = handle.readline(128).strip()
    except OSError:
        return ""
    match = _HOLDER_RE.match(text)
    if not match:
        return ""
    return f" (pid={match.group(1)} host={match.group(2)})"
