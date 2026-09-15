"""Файл VERSION — маркер синхронизированной версии хранилища в рабочей копии.

Формат повторяет upstream (``МенеджерСинхронизации.ЗаписатьФайлВерсийГит``):
XML-документ из одного элемента ``VERSION``. Отсутствующий или нечитаемый файл
трактуется как версия 0 — так же, как ``НомерСинхронизированнойВерсии``.
"""

from __future__ import annotations

import re
from pathlib import Path

VERSION_FILE_NAME = "VERSION"
AUTHORS_FILE_NAME = "AUTHORS"

_VERSION_RE = re.compile(r"<VERSION>\s*(.*?)\s*</VERSION>", re.DOTALL)


def version_file_path(work_dir: str | Path) -> Path:
    return Path(work_dir) / VERSION_FILE_NAME


def write_version_file(work_dir: str | Path, version: int | str = "0") -> Path:
    """Пишет VERSION атомарно: сначала временный файл рядом, затем замена."""
    path = version_file_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = '<?xml version="1.0" encoding="UTF-8"?>\n' f"<VERSION>{version}</VERSION>\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(path)
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
