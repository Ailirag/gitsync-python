"""Файл AUTHORS — сопоставление пользователей хранилища и подписей Git.

Формат upstream (``ПрочитатьФайлАвторов`` / ``ЗаписатьТаблицуПользователейВФайлАвторовGit``):
строки ``Автор=Представление``, строки, начинающиеся с ``//``, игнорируются,
строки без ровно одного разделителя пропускаются с предупреждением.
Подпись по умолчанию — ``Автор <Автор@домен>``.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("gitsync.authors")

DEFAULT_EMAIL_DOMAIN = "localhost"


def read_authors_file(path: str | Path) -> dict[str, str]:
    file = Path(path)
    if not file.is_file():
        return {}
    table: dict[str, str] = {}
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("//"):
            continue
        parts = line.split("=")
        if len(parts) != 2:
            if line.strip():
                log.warning("Ошибка чтения файла авторов, строка <%s>", line)
            continue
        table[parts[0].strip()] = parts[1].strip()
    return table


def author_signature(author: str, authors: dict[str, str], domain: str = DEFAULT_EMAIL_DOMAIN) -> str:
    """Подпись коммита для пользователя хранилища."""
    mapped = authors.get(author.strip())
    if mapped:
        return mapped
    name = author.strip()
    return f"{name} <{name}@{domain}>"


def render_primary_authors_file(authors: list[str], domain: str = DEFAULT_EMAIL_DOMAIN) -> str:
    lines = [f"{name}={name} <{name}@{domain}>" for name in authors]
    return "".join(line + "\n" for line in lines)


def write_primary_authors_file(
    path: str | Path, authors: list[str], domain: str = DEFAULT_EMAIL_DOMAIN
) -> Path:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(render_primary_authors_file(authors, domain), encoding="utf-8", newline="\n")
    return file
