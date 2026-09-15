"""Безопасная сборка путей внутри каталога.

Выгрузка и фикстуры — внешние данные: имя файла может содержать ``..`` или быть абсолютным.
Любая запись за пределы своего каталога считается ошибкой, а не «само пройдёт».
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath

from .errors import UnsafePathError


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
