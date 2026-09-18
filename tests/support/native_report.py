"""Сборка отчёта по версиям в формате MOXCEL для герметичных тестов и примеров.

Это **тестовая** утилита: продукт отчёты не пишет, их пишет конфигуратор. Контейнер
собирается по той же разметке ячеек, что и настоящий артефакт
(`tests/fixtures/native/repository-report-v1-v4.mxl`); эквивалентность по всем полям,
которые продукт читает, проверяется тестом `test_builder_reproduces_real_native_report`.

Открывается ли собранный здесь файл конфигуратором — НЕ проверялось и для тестов не нужно.
"""

from __future__ import annotations

from dataclasses import dataclass, field

HEADER = b"MOXCEL\x00\x08\x00\x01\x00\x0c\x00\xef\xbb\xbf"


@dataclass
class ReportVersion:
    number: int
    author: str
    date: str = ""
    time: str = ""
    comment: str = ""
    config_version: str = ""
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _cell(text: str, fmt: int = 3) -> str:
    if not text:
        return f"{{16,{fmt},\n{{1,0}},0}}"
    escaped = text.replace('"', '""')
    return f'{{16,{fmt},\n{{1,1,\n{{"#","{escaped}"}}\n}},0}}'


def _empty(fmt: int = 8) -> str:
    return f"{{0,{fmt}}}"


def build_report_mxl(versions: list[ReportVersion], storage_path: str = r"C:\storage") -> bytes:
    """Возвращает байты отчёта `/ConfigurationRepositoryReport` в формате MOXCEL."""
    cells: list[str] = [
        _cell(f"Отчет по версиям хранилища:    {storage_path}", 1),
        _cell("Дата отчета:", 2),
        _cell("15.09.2026"),
        _cell("Время отчета:", 2),
        _cell("12:35:47"),
        _empty(),
    ]
    for item in versions:
        cells += [
            _cell("Версия:", 2),
            _cell(str(item.number), 5),
            _cell("Пользователь:", 2),
            _cell(item.author),
            _cell("Дата создания:", 2),
            _cell(item.date),
            _cell("Время создания:", 2),
            _cell(item.time),
            _cell("Версия конфигурации:", 2),
            _cell(item.config_version),
            _cell("Комментарий:", 6),
            _cell(item.comment, 7),
            _empty(),
        ]
        for label, names in (
            ("Добавлены:", item.added),
            ("Изменены:", item.changed),
            ("Удалены:", item.removed),
        ):
            if not names:
                continue
            cells.append(_cell(label, 10))
            for index, name in enumerate(names):
                if index:
                    cells.append(_empty(13))
                cells.append(_cell(name, 11))
            cells.append(_empty())
    body = "{8,1,12,\n" + ",\n".join(cells) + "\n}\n"
    return HEADER + body.encode("utf-8")
