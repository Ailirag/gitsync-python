"""Слайс 1: файлы служебных метаданных VERSION/AUTHORS и разбор отчёта хранилища.

Поведение зафиксировано по исходнику upstream (см. docs/compatibility-matrix.md):
- VERSION пишется как XML-файл с единственным элементом <VERSION>;
- AUTHORS — строки вида ``Автор=Представление``, комментарии ``//`` пропускаются;
- первичный AUTHORS генерируется шаблоном ``%1=%1 <%1@домен>``.
"""

from __future__ import annotations

import datetime as dt

import pytest

from gitsync.authors import (
    author_signature,
    read_authors_file,
    render_primary_authors_file,
)
from gitsync.storage_report import StorageVersion, parse_storage_report
from gitsync.version_file import read_version_file, write_version_file


def test_version_file_roundtrip_uses_upstream_xml_shape(tmp_path):
    write_version_file(tmp_path, 17)

    raw = (tmp_path / "VERSION").read_text(encoding="utf-8")
    assert raw.splitlines()[0] == '<?xml version="1.0" encoding="UTF-8"?>'
    assert "<VERSION>17</VERSION>" in raw
    assert read_version_file(tmp_path) == 17


def test_version_file_missing_means_zero(tmp_path):
    assert read_version_file(tmp_path) == 0


def test_version_file_with_garbage_is_zero(tmp_path):
    (tmp_path / "VERSION").write_text("не число", encoding="utf-8")
    assert read_version_file(tmp_path) == 0


def test_authors_file_parsing_skips_comments_and_bad_lines(tmp_path):
    path = tmp_path / "AUTHORS"
    path.write_text(
        "// комментарий\n"
        "Иванов = Иван Иванов <ivanov@example.com>\n"
        "мусорная строка без разделителя\n"
        "Петров=Петров П <petrov@example.com>\n",
        encoding="utf-8",
    )

    table = read_authors_file(path)

    assert table == {
        "Иванов": "Иван Иванов <ivanov@example.com>",
        "Петров": "Петров П <petrov@example.com>",
    }


def test_author_signature_falls_back_to_default_domain(tmp_path):
    assert author_signature("Сидоров", {}, "example.org") == "Сидоров <Сидоров@example.org>"
    assert author_signature("Сидоров", {"Сидоров": "S <s@x.io>"}, "example.org") == "S <s@x.io>"


def test_primary_authors_file_matches_upstream_template():
    text = render_primary_authors_file(["Иванов", "Петров"], "example.org")
    assert text == "Иванов=Иванов <Иванов@example.org>\nПетров=Петров <Петров@example.org>\n"


def test_parse_storage_report_extracts_versions_authors_dates_comments():
    report = (
        "﻿Отчет по версиям хранилища конфигурации\n"
        "\n"
        "Версия:                  1\n"
        "Пользователь:            Иванов\n"
        "Дата создания:           15.09.2026 10:20:30\n"
        "Комментарий:             Первая версия\n"
        "\n"
        "Версия:                  2\n"
        "Пользователь:            Петров\n"
        "Дата создания:           16.09.2026 08:00:00\n"
        "Комментарий:             Вторая версия\n"
        "                         вторая строка комментария\n"
        "\n"
    )

    versions = parse_storage_report(report)

    assert versions == [
        StorageVersion(
            number=1,
            author="Иванов",
            date=dt.datetime(2026, 9, 15, 10, 20, 30),
            comment="Первая версия",
        ),
        StorageVersion(
            number=2,
            author="Петров",
            date=dt.datetime(2026, 9, 16, 8, 0, 0),
            comment="Вторая версия\nвторая строка комментария",
        ),
    ]


def test_parse_storage_report_tolerates_thousand_separators_in_numbers():
    report = (
        "Версия:                  1 234\n"
        "Пользователь: Иванов\n"
        "Дата создания: 15.09.2026 10:20:30\n"
        "Комментарий:\n"
    )
    assert parse_storage_report(report)[0].number == 1234


def test_parse_storage_report_rejects_empty_input():
    with pytest.raises(ValueError):
        parse_storage_report("   \n")
