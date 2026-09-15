"""Файл VERSION — маркер синхронизированной версии хранилища в рабочей копии.

Формат повторяет upstream (``МенеджерСинхронизации.ЗаписатьФайлВерсийГит``):
XML-документ из одного элемента ``VERSION``. Отсутствующий или нечитаемый файл
трактуется как версия 0 — так же, как ``НомерСинхронизированнойВерсии``.
"""

from __future__ import annotations

import re
from pathlib import Path

from .errors import VersionFileError

VERSION_FILE_NAME = "VERSION"
AUTHORS_FILE_NAME = "AUTHORS"

_VERSION_RE = re.compile(r"<VERSION>\s*(.*?)\s*</VERSION>", re.DOTALL)


def version_file_path(work_dir: str | Path) -> Path:
    return Path(work_dir) / VERSION_FILE_NAME


def write_version_file(work_dir: str | Path, version: int | str = "0") -> Path:
    """Пишет VERSION атомарно: сначала временный файл рядом, затем замена."""
    import base64
    import os

    from .transaction import owned_path, put_image

    path = owned_path(Path(work_dir), VERSION_FILE_NAME)
    body = '<?xml version="1.0" encoding="UTF-8"?>\n' f"<VERSION>{version}</VERSION>\n"
    put_image(path, {'data': base64.b64encode(body.encode()).decode(),
                     'mode': 0o666 if os.name == 'nt' else 0o644})
    return path


def read_version_file(work_dir: str | Path) -> int:
    path = version_file_path(work_dir)
    if not path.is_file():
        return 0
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return 0
    match = _VERSION_RE.search(raw)
    text = match.group(1) if match else raw.strip()
    try:
        return int(text.replace(" ", "").replace(" ", ""))
    except ValueError:
        return 0


def read_version_file_strict(work_dir: str | Path) -> int:
    """Как :func:`read_version_file`, но нечитаемый маркер — ошибка, а не 0.

    Отличие от upstream осознанное: ``НомерСинхронизированнойВерсии`` молча отдаёт 0, и
    испорченный маркер означал бы повтор всей истории поверх существующего репозитория.
    Отсутствие файла обрабатывает вызывающий код (:class:`gitsync.sync.SyncManager`): в пустой
    рабочей копии это законное начало с нуля, в копии с историей — авария.
    """
    path = version_file_path(work_dir)
    raw = path.read_text(encoding="utf-8-sig")
    match = _VERSION_RE.search(raw)
    text = match.group(1) if match else raw.strip()
    digits = "".join(ch for ch in text if not ch.isspace())
    try:
        value = int(digits)
    except ValueError:
        raise VersionFileError(
            f"Файл <{path}> не содержит номер версии: {raw.strip()[:120]!r}. "
            "Исправьте файл или задайте номер командой set-version."
        ) from None
    if value < 0:
        raise VersionFileError(f"Отрицательный номер версии в <{path}>: {value}")
    return value
