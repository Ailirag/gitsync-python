"""Безопасная сборка путей внутри каталога.

Выгрузка и фикстуры — внешние данные: имя файла может содержать ``..`` или быть абсолютным.
Любая запись за пределы своего каталога считается ошибкой, а не «само пройдёт».
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePath

from .errors import UnsafePathError


def reject_linked_path(path: str | Path) -> None:
    """Reject links/reparse points in the lexical target and every existing ancestor.

    Do not resolve first: that hides the link. lstat supports NTFS junctions on 3.11.
    This is a preflight containment check, not a filesystem TOCTOU lock.
    """
    target = Path(path).absolute()
    for item in [target, *target.parents]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or (os.name == 'nt' and (
                    info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or info.st_reparse_tag))):
            raise UnsafePathError('Linked transaction path')


def safe_join(root: str | Path, relative: str | PurePath) -> Path:
    """Возвращает путь внутри ``root`` или падает с :class:`UnsafePathError`."""
    root_path = Path(root)
    candidate = PurePath(relative)
    if candidate.is_absolute() or (os.name == "nt" and PurePath(str(relative)).drive):
        raise UnsafePathError(f"Абсолютный путь внутри выгрузки недопустим: {relative}")

    root_resolved = os.path.normcase(os.path.normpath(os.path.abspath(str(root_path))))
    target = os.path.normcase(os.path.normpath(os.path.abspath(str(root_path / candidate))))
    if target != root_resolved and not target.startswith(root_resolved + os.sep):
        raise UnsafePathError(f"Путь <{relative}> выходит за пределы каталога <{root_path}>")
    return Path(os.path.normpath(str(root_path / candidate)))
