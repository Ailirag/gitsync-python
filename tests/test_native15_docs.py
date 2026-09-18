"""Native15: руководство обязано описывать то, что проверено на живой платформе.

Три вещи стоили этой итерации целого прогона, и ни одной из них не было в
документации:

* лицензию в Linux платформа ищет в ``/var/1C/licenses`` — проверено детектором чтения
  (там файл прочитан, в остальных предполагаемых каталогах — нет);
* программная лицензия, привязанная к другому компьютеру, даёт пользователю
  «Не найдена лицензия», а настоящую причину («Ошибка привязки программной лицензии
  к компьютеру») видно только в технологическом журнале;
* причину отказа конфигуратор пишет в файл ``/Out``, а не в stdout/stderr.

Плюс два свойства окружения, каждое из которых ломает работу молча: неизвестный UID
(платформа падает по SIGSEGV) и недоступный на запись HOME (шум подсистемы шрифтов).
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "docker-guide.md"


@pytest.fixture(scope="module")
def guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def test_guide_names_the_real_license_directory(guide):
    """Каталог поиска лицензии назван точно — гадать оператору не по чему."""
    assert "/var/1C/licenses" in guide


def test_guide_warns_that_binding_is_to_a_computer(guide):
    """Скопировать чужой .lic нельзя: привязка к машине — главная ловушка."""
    lowered = guide.lower()
    assert "привязк" in lowered
    assert "ошибка привязки программной лицензии" in lowered, (
        "в руководстве должно быть настоящее сообщение платформы, по нему ищут причину"
    )


def test_guide_explains_how_to_see_the_real_license_reason(guide):
    """Пользователю платформа говорит «не найдена», причину прячет в техжурнал."""
    assert "logcfg.xml" in guide
    assert "LIC" in guide
    assert "/opt/1cv8/conf" in guide, "надо назвать каталог, куда класть logcfg.xml"


def test_guide_says_reason_of_a_failed_call_is_in_the_out_file(guide):
    assert "/Out" in guide
    lowered = guide.lower()
    assert "stderr" in lowered


def test_guide_describes_the_arbitrary_uid_crash_and_the_way_out(guide):
    """Неизвестный UID роняет платформу по SIGSEGV — это обязано быть написано."""
    assert "139" in guide
    assert "/etc/passwd" in guide
    assert "onec-user-entrypoint" in guide


def test_guide_requires_writable_home_for_the_font_cache(guide):
    """Переопределили HOME — дайте записываемый кэш, иначе шум в каждом запуске."""
    assert "XDG_CACHE_HOME" in guide
    assert "Fontconfig" in guide


def test_guide_does_not_promise_native_storage_work_without_a_license(guide):
    """Проверка честности: раздел про хранилище по-прежнему не объявлен пройденным."""
    assert "НЕ ПРОВЕРЕНО" in guide
