"""Регрессия на РЕАЛЬНЫЙ отчёт конфигуратора: MOXCEL/MXL, а не выдуманный текст."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from gitsync.storage_report import authors_from_report, parse_storage_report
from support.native_report import ReportVersion, build_report_mxl

FIXTURE = Path(__file__).parent / "fixtures" / "native" / "repository-report-v1-v4.mxl"
FIXTURE_SHA256 = "5f941c82d7e8bee7178a69e58a5c54e647c685ed5a7e9cbb7bb06a8e89f055e7"


def test_fixture_is_the_recorded_native_artifact():
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256
    assert raw.startswith(b"MOXCEL"), "фикстура должна быть настоящим отчётом конфигуратора"


def test_parses_real_native_report():
    versions = parse_storage_report(FIXTURE.read_bytes())

    assert [item.number for item in versions] == [1, 2, 3, 4]
    assert {item.author for item in versions} == {"acceptance"}
    assert [item.comment for item in versions] == [
        "Создание хранилища конфигурации",
        "acceptance: add synthetic AcceptanceProbe module",
        "acceptance: modify marker; unicode проверка",
        "acceptance: delete synthetic AcceptanceProbe module",
    ]
    assert [item.date for item in versions] == [
        dt.datetime(2026, 9, 15, 12, 28, 36),
        dt.datetime(2026, 9, 15, 12, 33, 30),
        dt.datetime(2026, 9, 15, 12, 34, 1),
        dt.datetime(2026, 9, 15, 12, 34, 5),
    ]
    assert authors_from_report(versions) == ["acceptance"]


def test_parses_changed_objects_of_real_report():
    versions = {item.number: item for item in parse_storage_report(FIXTURE.read_bytes())}

    assert "Конфигурация" in versions[1].added
    assert versions[2].added == ("ОбщийМодуль.AcceptanceProbe",)
    assert "ОбщийМодуль.AcceptanceProbe" in versions[3].changed
    assert versions[4].removed == ("ОбщийМодуль.AcceptanceProbe",)
    assert versions[2].removed == ()


def test_builder_reproduces_real_native_report():
    """Сборщик фикстур даёт те же записи, что настоящий отчёт конфигуратора.

    Это условие, при котором герметичные тесты на построенном MOXCEL что-то доказывают:
    поля, которые читает продукт, совпадают с разобранными из native-артефакта.
    """
    native = parse_storage_report(FIXTURE.read_bytes())
    rebuilt = parse_storage_report(
        build_report_mxl(
            [
                ReportVersion(
                    number=item.number,
                    author=item.author,
                    date=item.date.strftime("%d.%m.%Y"),
                    time=item.date.strftime("%H:%M:%S"),
                    comment=item.comment,
                    added=list(item.added),
                    changed=list(item.changed),
                    removed=list(item.removed),
                )
                for item in native
            ]
        )
    )

    assert rebuilt == native
    for built, real in zip(rebuilt, native, strict=True):
        assert (built.added, built.changed, built.removed) == (real.added, real.changed, real.removed)


def test_rejects_invented_plain_text_report():
    """Раньше парсер принимал выдуманную построчную разметку — это был ложный контракт."""
    invented = "Версия: 7\nПользователь: ivan\nДата создания: 15.09.2026 12:00:00\nКомментарий: тест\n"
    with pytest.raises(ValueError, match="MOXCEL"):
        parse_storage_report(invented)


def test_rejects_empty_and_garbage():
    with pytest.raises(ValueError):
        parse_storage_report(b"")
    with pytest.raises(ValueError, match="MOXCEL"):
        parse_storage_report(b"\x00\x01\x02\x03")


EMPTY_RANGE = FIXTURE.parent / "repository-report-empty-range.mxl"
EMPTY_RANGE_SHA256 = "a22f106ac800293d9fa8af0ae402e78ef883c691fa38bac7f94b3bf66b8fd3e8"


def test_empty_version_range_is_not_an_error():
    """Повторный запуск запрашивает `-NBegin <текущая+1>` и получает отчёт без версий.

    Настоящий конфигуратор на диапазоне 5..5 при максимуме 4 отвечает кодом 0 и строит отчёт
    («Отчет успешно построен») с одной лишь шапкой. Это штатное «новых версий нет», а не сбой:
    раньше парсер валил на этом весь повторный прогон.
    """
    raw = EMPTY_RANGE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EMPTY_RANGE_SHA256
    assert parse_storage_report(raw) == []


def test_report_without_header_is_still_an_error():
    """Пустой MOXCEL без шапки отчёта — не «нет версий», а испорченный артефакт."""
    from support.native_report import HEADER

    with pytest.raises(ValueError, match="не найдено ни одной версии"):
        parse_storage_report(HEADER + b"{8,1,12,\n{0,0}\n}\n")
