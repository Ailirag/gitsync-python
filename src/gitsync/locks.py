"""Эксклюзивная блокировка целевого репозитория.

Блокировка межпроцессная: файл создаётся атомарно (``O_CREAT|O_EXCL``), внутри — pid и хост,
что помогает разобраться, кто держит цель. Ожидание ограничено таймаутом.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path

from .errors import LockBusyError


@contextlib.contextmanager
def exclusive_lock(path: str | Path, timeout: float = 30.0, poll: float = 0.05) -> Iterator[Path]:
    """Держит эксклюзивную блокировку ``path`` или падает с :class:`LockBusyError`."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(timeout, 0.0)
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                holder = _read_holder(lock_path)
                raise LockBusyError(
                    f"Цель <{lock_path}> уже обрабатывается{holder}; "
                    f"ожидание {timeout:g} с истекло"
                ) from None
            time.sleep(poll)

    try:
        os.write(fd, f"pid={os.getpid()} host={socket.gethostname()}\n".encode())
        os.close(fd)
        fd = None
        yield lock_path
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            lock_path.unlink()


def _read_holder(lock_path: Path) -> str:
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return f" ({text})" if text else ""
