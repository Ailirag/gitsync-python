"""В содержимом репозитория не должно быть внутренних путей и имён машин.

Репозиторий готовится к передаче заказчику и, отдельным решением владельца, к
публикации. Внутренняя раскладка рабочей машины и имена хостов стенда — не секрет
уровня пароля, но и не то, что стоит отдавать наружу: по ним видно чужую сеть и
чужие каталоги, а пользы читателю ноль.

Проверяется **текущее содержимое** рабочего дерева. История Git этой проверкой не
затрагивается: переписывать её — отдельное решение владельца репозитория.

Маркеры собраны из кусков намеренно: иначе сам этот файл стал бы тем самым
раскрытием, которое он запрещает.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Каталоги, которых нет в поставке: окружения, кэши, сборочный мусор, история Git.
SKIP_DIRS = {".git", ".venv", "venv", "dist", "build", "__pycache__",
             ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}
#: Двоичные вложения читать как текст бессмысленно.
SKIP_SUFFIXES = {".cf", ".cfe", ".mxl", ".png", ".jpg", ".ico", ".whl", ".zip", ".exe"}

MARKERS = (
    "d:/" + "hermes",
    "d:\\" + "hermes",
    "c:/users/" + "to101",
    "c:\\users\\" + "to101",
    "cere" + "bro",
    "192." + "168.",
)


def _files() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue  # сам список маркеров
        found.append(path)
    return found


@pytest.mark.parametrize("marker", MARKERS)
def test_no_internal_paths_or_hostnames_in_the_tree(marker):
    попались = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if marker in line.lower():
                попались.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    assert not попались, f"внутренняя ссылка «{marker}» в: {попались[:10]}"


def test_upstream_is_named_by_a_public_address():
    """Матрица совместимости должна ссылаться на публичный upstream, а не на диск автора."""
    matrix = (ROOT / "docs" / "compatibility-matrix.md").read_text(encoding="utf-8")
    assert "https://github.com/oscript-library/gitsync" in matrix
    assert "82d87f54942400362e3950d8190f9323bcb883c0" in matrix, "нужен точный коммит сравнения"
