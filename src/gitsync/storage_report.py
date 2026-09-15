"""Разбор отчёта по версиям хранилища конфигурации 1С (``/ConfigurationRepositoryReport``).

Конфигуратор пишет отчёт как текстовый файл с блоками на версию; upstream разбирает его
библиотекой ``v8storage`` и берёт из неё поля ``Номер``/``Автор``/``Дата``/``Комментарий``
(см. ``МенеджерСинхронизации.ПрочитатьТаблицуИсторииХранилища``), причём теги в отчёте
не предоставляются.

ВАЖНО (нерешённый контракт): точная локализованная разметка отчёта здесь воспроизведена
по русскоязычному варианту (upstream форсирует ``/L RU``) и НЕ верифицирована на живой
платформе в этой итерации. Парсер намеренно устойчив: ключ берётся до первого двоеточия,
распознаются RU/EN подписи, продолжения комментария подклеиваются к предыдущей строке.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

# Подписи полей: слева — то, что печатает конфигуратор, справа — наше каноническое поле.
_FIELD_ALIASES = {
    "версия": "number",
    "version": "number",
    "пользователь": "author",
    "user": "author",
    "автор": "author",
    "дата создания": "date",
    "дата": "date",
    "date": "date",
    "created": "date",
    "комментарий": "comment",
    "comment": "comment",
}

_DATE_FORMATS = (
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
)

_KEY_RE = re.compile(r"^(?P<key>[^:]{1,40}):(?P<value>.*)$")


@dataclass(frozen=True, order=True)
class StorageVersion:
    """Строка истории хранилища: номер, автор, дата и комментарий версии."""

    number: int
    author: str
    date: dt.datetime | None
    comment: str

    @property
    def tag(self) -> str:
        # Теги в отчёте конфигуратора не предоставляются (так же в upstream).
        return ""


def parse_number(text: str) -> int:
    cleaned = text.replace(" ", "").replace(" ", "").strip()
    return int(cleaned)


def parse_report_date(text: str) -> dt.datetime | None:
    value = text.strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def parse_storage_report(text: str) -> list[StorageVersion]:
    """Разбирает текст отчёта в список версий, отсортированный по номеру."""
    if not text or not text.strip():
        raise ValueError("Пустой отчёт по версиям хранилища конфигурации")

    versions: list[StorageVersion] = []
    current: dict[str, object] | None = None
    last_field: str | None = None

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿").split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            last_field = None
            continue

        match = _KEY_RE.match(line.strip())
        field = None
        value = ""
        if match:
            field = _FIELD_ALIASES.get(match.group("key").strip().lower())
            value = match.group("value").strip()

        if field == "number":
            if current is not None:
                versions.append(_finish(current))
            current = {"number": parse_number(value), "author": "", "date": None, "comment": ""}
            last_field = "number"
            continue

        if current is None:
            continue

        if field == "author":
            current["author"] = value
            last_field = "author"
        elif field == "date":
            current["date"] = parse_report_date(value)
            last_field = "date"
        elif field == "comment":
            current["comment"] = value
            last_field = "comment"
        elif last_field == "comment":
            # Многострочный комментарий: продолжение печатается с отступом без подписи поля.
            existing = str(current["comment"])
            current["comment"] = (existing + "\n" + line.strip()).strip("\n")

    if current is not None:
        versions.append(_finish(current))

    if not versions:
        raise ValueError("В отчёте по версиям не найдено ни одной версии")

    return sorted(versions, key=lambda item: item.number)


def _finish(data: dict[str, object]) -> StorageVersion:
    return StorageVersion(
        number=int(data["number"]),  # type: ignore[arg-type]
        author=str(data["author"]),
        date=data["date"],  # type: ignore[arg-type]
        comment=str(data["comment"]),
    )


def authors_from_report(versions: list[StorageVersion]) -> list[str]:
    """Уникальные авторы в порядке первого появления (аналог ``ПолучитьАвторов``)."""
    seen: dict[str, None] = {}
    for version in versions:
        if version.author:
            seen.setdefault(version.author, None)
    return list(seen)
