"""Разбор табличного документа 1С в текстовой сериализации (MOXCEL, `*.mxl`).

Зачем это здесь: `/ConfigurationRepositoryReport` **всегда** пишет табличный документ, даже
когда файлу задано расширение `.txt`. Это проверено на живом конфигураторе 8.3.27.2130
(см. `tests/fixtures/native/PROVENANCE.md`), поэтому построчного текстового отчёта, который
можно было бы читать регулярками, не существует.

Формат файла::

    MOXCEL\\0\\x08\\0\\x01\\0\\x0c\\0   двоичный заголовок
    <BOM UTF-8>                        далее — текст
    {8,1,12, ... }                     вложенные списки, элементы через запятую

Элементы списка: целые/дробные числа, строки в двойных кавычках (внутренняя кавычка
удваивается, переводы строк допустимы), UUID и прочие голые токены.

Ячейка кодируется одним из двух видов:

* ``{16,<формат>,{1,1,{"#","текст"}},0}`` — ячейка с текстом;
* ``{16,<формат>,{1,0},0}`` и ``{0,<формат>}`` — пустая ячейка.

Второй элемент — индекс *формата*, а не текста: один и тот же индекс встречается с разными
значениями. Поэтому текст берётся из узла ``{"#","текст"}`` строго из двух элементов —
служебные секции в конце документа (именованные области, высоты строк) под это не подпадают.
"""

from __future__ import annotations

from dataclasses import dataclass

MOXCEL_SIGNATURE = b"MOXCEL"
_BOM_UTF8 = b"\xef\xbb\xbf"


class MxlFormatError(ValueError):
    """Файл не является табличным документом MOXCEL или повреждён."""


@dataclass(frozen=True)
class MxlCell:
    """Ячейка табличного документа: текст (может быть пустым) и индекс формата."""

    text: str
    fmt: int


def decode_container(raw: bytes) -> str:
    """Отрезает двоичный заголовок MOXCEL и возвращает текстовое тело."""
    if not raw:
        raise MxlFormatError("Пустой отчёт по версиям хранилища конфигурации")
    if not raw.startswith(MOXCEL_SIGNATURE):
        raise MxlFormatError(
            "Отчёт по версиям не является табличным документом MOXCEL: "
            f"первые байты {raw[:12]!r}. Конфигуратор пишет MOXCEL даже в файл с расширением .txt"
        )
    start = raw.find(_BOM_UTF8)
    if start >= 0:
        return raw[start + len(_BOM_UTF8) :].decode("utf-8")
    brace = raw.find(b"{")
    if brace < 0:
        raise MxlFormatError("В отчёте MOXCEL нет тела документа")
    return raw[brace:].decode("utf-8", "replace")


def parse_values(text: str) -> list:
    """Разбирает тело документа в список значений (вложенные списки, числа, строки)."""
    values: list = []
    pos = 0
    length = len(text)
    while pos < length:
        char = text[pos]
        if char in " \t\r\n,":
            pos += 1
            continue
        value, pos = _parse_value(text, pos)
        values.append(value)
    return values


def _parse_value(text: str, pos: int) -> tuple[object, int]:
    char = text[pos]
    if char == "{":
        return _parse_list(text, pos)
    if char == '"':
        return _parse_string(text, pos)
    return _parse_atom(text, pos)


def _parse_list(text: str, pos: int) -> tuple[list, int]:
    pos += 1  # "{"
    items: list = []
    length = len(text)
    while pos < length:
        char = text[pos]
        if char in " \t\r\n,":
            pos += 1
            continue
        if char == "}":
            return items, pos + 1
        value, pos = _parse_value(text, pos)
        items.append(value)
    raise MxlFormatError("Незакрытый список в теле MOXCEL")


def _parse_string(text: str, pos: int) -> tuple[str, int]:
    pos += 1  # открывающая кавычка
    chunks: list[str] = []
    length = len(text)
    while pos < length:
        char = text[pos]
        if char == '"':
            if pos + 1 < length and text[pos + 1] == '"':
                chunks.append('"')
                pos += 2
                continue
            return "".join(chunks), pos + 1
        chunks.append(char)
        pos += 1
    raise MxlFormatError("Незакрытая строка в теле MOXCEL")


def _parse_atom(text: str, pos: int) -> tuple[object, int]:
    start = pos
    length = len(text)
    while pos < length and text[pos] not in ",{}\r\n":
        pos += 1
    token = text[start:pos].strip()
    try:
        return int(token), pos
    except ValueError:
        pass
    try:
        return float(token), pos
    except ValueError:
        return token, pos


def iter_cells(values: list) -> list[MxlCell]:
    """Ячейки документа в порядке чтения, включая пустые."""
    cells: list[MxlCell] = []
    _collect_cells(values, cells)
    return cells


def _collect_cells(node: object, cells: list[MxlCell]) -> None:
    if not isinstance(node, list):
        return
    if len(node) >= 3 and node[0] == 16 and isinstance(node[1], int):
        cells.append(MxlCell(text=_cell_text(node), fmt=node[1]))
        return
    if len(node) == 2 and node[0] == 0 and isinstance(node[1], int):
        cells.append(MxlCell(text="", fmt=node[1]))
        return
    for item in node:
        _collect_cells(item, cells)


def _cell_text(node: list) -> str:
    """Текст ячейки: строго узел ``{"#", "текст"}`` из двух элементов."""
    if len(node) == 2 and node[0] == "#" and isinstance(node[1], str):
        return node[1]
    for item in node:
        if isinstance(item, list):
            found = _cell_text(item)
            if found:
                return found
    return ""


def read_cells(raw: bytes) -> list[MxlCell]:
    """Полный путь: байты файла отчёта → ячейки в порядке чтения."""
    return iter_cells(parse_values(decode_container(raw)))
