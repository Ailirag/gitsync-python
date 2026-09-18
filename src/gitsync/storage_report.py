"""Разбор отчёта по версиям хранилища конфигурации 1С (``/ConfigurationRepositoryReport``).

Конфигуратор отдаёт отчёт **табличным документом MOXCEL**, а не текстом: это проверено
запуском 8.3.27.2130 на native-стенде, расширение `.txt` формат не меняет (провенанс —
`tests/fixtures/native/PROVENANCE.md`). Поэтому сначала разбирается контейнер
(:mod:`gitsync.mxl`), а уже потом ячейки читаются по разметке отчёта.

Разметка отчёта (подтверждена на реальном артефакте, ``/L RU``)::

    Отчет по версиям хранилища:  <путь>
    Дата отчета: <дата>     Время отчета: <время>
    Версия: <N>
    Пользователь: <автор>
    Дата создания: <дата>   Время создания: <время>
    Версия конфигурации: <строка или пусто>
    Комментарий: <текст>
    Добавлены: / Изменены: / Удалены:   <имена объектов по одному в ячейке>

Значение всегда лежит в ячейке, следующей за ячейкой-подписью, и может быть пустым
(например «Версия конфигурации:»), поэтому пустые ячейки из потока не выбрасываются.

Английские подписи добавлены как запасной вариант и на живой платформе НЕ проверялись:
upstream форсирует ``/L RU``, так и делает :class:`gitsync.designer.DesignerRunner`.
Теги отчёт не содержит — как и в upstream.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .mxl import MxlFormatError, read_cells

# Подписи полей: слева — то, что печатает конфигуратор, справа — наше каноническое поле.
_FIELD_LABELS = {
    "версия:": "number",
    "version:": "number",
    "пользователь:": "author",
    "user:": "author",
    "дата создания:": "date",
    "creation date:": "date",
    "время создания:": "time",
    "creation time:": "time",
    "комментарий:": "comment",
    "comment:": "comment",
    "версия конфигурации:": "config_version",
    "configuration version:": "config_version",
}

_SECTION_LABELS = {
    "добавлены:": "added",
    "added:": "added",
    "изменены:": "changed",
    "changed:": "changed",
    "удалены:": "removed",
    "deleted:": "removed",
    "removed:": "removed",
}

_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y")
_TIME_FORMATS = ("%H:%M:%S", "%H:%M")


@dataclass(frozen=True, order=True)
class StorageVersion:
    """Строка истории хранилища: номер, автор, дата, комментарий и состав изменений."""

    number: int
    author: str
    date: dt.datetime | None
    comment: str
    added: tuple[str, ...] = field(default=(), compare=False)
    changed: tuple[str, ...] = field(default=(), compare=False)
    removed: tuple[str, ...] = field(default=(), compare=False)
    config_version: str = field(default="", compare=False)

    @property
    def tag(self) -> str:
        # Теги в отчёте конфигуратора не предоставляются (так же в upstream).
        return ""


def parse_number(text: str) -> int:
    cleaned = text.replace(" ", "").replace(" ", "").strip()
    return int(cleaned)


def parse_report_date(date_text: str, time_text: str = "") -> dt.datetime | None:
    """Собирает дату версии из ячеек «Дата создания» и «Время создания»."""
    value = date_text.strip()
    if not value:
        return None
    parsed: dt.date | None = None
    for fmt in _DATE_FORMATS:
        try:
            parsed = dt.datetime.strptime(value, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    moment = dt.time()
    clock = time_text.strip()
    for fmt in _TIME_FORMATS:
        try:
            moment = dt.datetime.strptime(clock, fmt).time()
            break
        except ValueError:
            continue
    return dt.datetime.combine(parsed, moment)


def parse_storage_report(report: bytes | str) -> list[StorageVersion]:
    """Разбирает native-отчёт конфигуратора в список версий, отсортированный по номеру."""
    raw = report.encode("utf-8") if isinstance(report, str) else bytes(report)
    if not raw.strip():
        raise ValueError("Пустой отчёт по версиям хранилища конфигурации")
    try:
        cells = read_cells(raw)
    except MxlFormatError as exc:
        raise ValueError(str(exc)) from exc

    versions: list[StorageVersion] = []
    current: dict[str, object] | None = None
    pending: str | None = None
    section: str | None = None

    for cell in cells:
        text = cell.text.strip()
        key = text.lower()

        if pending is not None:
            if current is not None:
                _apply(current, pending, cell.text)
            pending = None
            continue

        field_name = _FIELD_LABELS.get(key)
        if field_name is not None:
            section = None
            if field_name == "number":
                if current is not None:
                    versions.append(_finish(current))
                current = _new_record()
            pending = field_name
            continue

        section_name = _SECTION_LABELS.get(key)
        if section_name is not None:
            section = section_name if current is not None else None
            continue

        if current is not None and section is not None and text:
            objects: list[str] = current[section]  # type: ignore[assignment]
            objects.append(text)

    if current is not None:
        versions.append(_finish(current))

    if not versions:
        if any(_is_report_header(cell.text) for cell in cells):
            # Штатный случай: запрошен диапазон выше максимальной версии (повторный запуск).
            # Конфигуратор отвечает кодом 0 и строит отчёт из одной шапки.
            return []
        raise ValueError("В отчёте по версиям не найдено ни одной версии")

    return sorted(versions, key=lambda item: item.number)


_REPORT_HEADERS = ("отчет по версиям хранилища", "configuration repository report")


def _is_report_header(text: str) -> bool:
    lowered = text.strip().lower()
    return any(lowered.startswith(marker) for marker in _REPORT_HEADERS)


def _new_record() -> dict[str, object]:
    return {
        "number": 0,
        "author": "",
        "date": "",
        "time": "",
        "comment": "",
        "config_version": "",
        "added": [],
        "changed": [],
        "removed": [],
    }


def _apply(record: dict[str, object], field_name: str, value: str) -> None:
    if field_name == "number":
        record["number"] = parse_number(value)
    elif field_name == "comment":
        # Комментарий берётся как есть: многострочный лежит в одной ячейке.
        record["comment"] = value.strip("\r\n")
    else:
        record[field_name] = value.strip()


def _finish(data: dict[str, object]) -> StorageVersion:
    return StorageVersion(
        number=int(data["number"]),  # type: ignore[arg-type]
        author=str(data["author"]),
        date=parse_report_date(str(data["date"]), str(data["time"])),
        comment=str(data["comment"]),
        added=tuple(data["added"]),  # type: ignore[arg-type]
        changed=tuple(data["changed"]),  # type: ignore[arg-type]
        removed=tuple(data["removed"]),  # type: ignore[arg-type]
        config_version=str(data["config_version"]),
    )


def authors_from_report(versions: list[StorageVersion]) -> list[str]:
    """Уникальные авторы в порядке первого появления (аналог ``ПолучитьАвторов``)."""
    seen: dict[str, None] = {}
    for version in versions:
        if version.author:
            seen.setdefault(version.author, None)
    return list(seen)
