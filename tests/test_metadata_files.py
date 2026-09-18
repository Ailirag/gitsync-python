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
from support.native_report import ReportVersion, build_report_mxl


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
    report = build_report_mxl(
        [
            ReportVersion(1, "Иванов", "15.09.2026", "10:20:30", "Первая версия"),
            ReportVersion(
                2,
                "Петров",
                "16.09.2026",
                "08:00:00",
                "Вторая версия\nвторая строка комментария",
            ),
        ]
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
    report = build_report_mxl([ReportVersion(0, "Иванов", "15.09.2026", "10:20:30")])
    # В табличном документе большие номера конфигуратор печатает с разделителем групп.
    report = report.replace(b'"0"', b'"1 234"')
    assert parse_storage_report(report)[0].number == 1234


def test_parse_storage_report_keeps_empty_comment_and_config_version():
    report = build_report_mxl([ReportVersion(5, "Иванов", "15.09.2026", "10:20:30", comment="")])
    version = parse_storage_report(report)[0]
    assert version.number == 5
    assert version.comment == ""
    assert version.config_version == ""


def test_parse_storage_report_rejects_empty_input():
    with pytest.raises(ValueError):
        parse_storage_report("   \n")
