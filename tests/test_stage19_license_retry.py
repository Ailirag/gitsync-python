"""stage-19: временный отказ ЛИЦЕНЗИИ — это не фатальная ошибка задания.

ЧТО ЗАМЕРЕНО НА СТЕНДЕ (evidence/stage-19). Сервис лицензий отвечает отказом, когда
одновременных сеансов конфигуратора больше, чем он готов выдать В ЭТУ СЕКУНДУ. Число
одновременно получаемых лицензий на стенде НЕ постоянно: в пределах десяти минут
замерено от 2 до 8 и более при одинаковом клиенте и одинаковых параметрах. Отказ
приходит В ДВУХ формулировках, и обе — про выдачу лицензии, а не про работу:

    «Не обнаружено свободной лицензии!»            / "There are no free licenses!"
    «Не найдена лицензия. Не обнаружен ключ...»    / "License not found. Software
                                                     protection key ... not found!"

Обе воспроизведены одним и тем же зондом, где менялось ТОЛЬКО число одновременных
конфигураторов (evidence/stage-19/12-ladder.log, 14-churn1-summary.txt).

ЧТО ИЗ ЭТОГО СЛЕДУЕТ ДЛЯ КОДА. ``jobs=2`` просит две лицензии разом вместо одной, и
на этом стенде запас иногда равен нулю. Продукт обязан отличать «лицензию сейчас не
дали» от «неверный пользователь хранилища»: первое проходит само, второе ожиданием не
лечится. Сейчас ``_export_one`` не повторяет НИ ОДИН ``DesignerError``, поэтому
настроенный оператором ``retries`` к самому частому временному отказу на этом стенде
не применяется вовсе, и одна несостоявшаяся выдача лицензии стоит всех оставшихся
версий хранилища.

ЧЕГО ЭТА ПРАВКА НЕ ДЕЛАЕТ. Она НЕ является устранением причины: причина — снаружи, в
доступности лицензий, и повтором она не устраняется. Она не делает ``jobs=2``
подтверждённой конфигурацией и не заменяет очередь допуска.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

from gitsync.backends import FakeStorageBackend
from gitsync.designer import DesignerRunner
from gitsync.errors import DesignerError, LicenseUnavailableError
from gitsync.gitrepo import GitRepo
from gitsync.storage_report import StorageVersion
from gitsync.sync import SyncManager, SyncOptions

#: Подставной «конфигуратор»: причину кладёт ТОЛЬКО в /Out, как настоящий.
FAKE_CONFIGURATOR = """
import sys, pathlib
args = sys.argv[1:]
out = None
for index, item in enumerate(args):
    if item == "/Out" and index + 1 < len(args):
        out = args[index + 1]
reason = args[-1]
if out:
    pathlib.Path(out).write_text("\\ufeff" + reason + "\\n", encoding="utf-8")
sys.stderr.write("Fontconfig error: No writable cache directories\\n")
sys.exit(1)
"""

# Сырые тексты, снятые со стенда, а не придуманные.
NO_FREE_RU = "Не обнаружено свободной лицензии!"
NO_FREE_EN = "There are no free licenses!"
NO_KEY_RU = ("Не найдена лицензия. Не обнаружен ключ защиты программы или полученная "
             "программная лицензия!")
NO_KEY_EN = ("License not found. Software protection key or acquired software license "
             "not found!")
AUTH_RU = "Ошибка аутентификации в хранилище конфигурации!"


@pytest.fixture()
def fake_v8(tmp_path) -> Path:
    script = tmp_path / "fake_configurator.py"
    script.write_text(FAKE_CONFIGURATOR, encoding="utf-8")
    return script


def _fail_with(runner: DesignerRunner, fake_v8: Path, reason: str) -> DesignerError:
    out = runner.out_file()
    with pytest.raises(DesignerError) as excinfo:
        runner.run([sys.executable, str(fake_v8), "DESIGNER", "/Out", str(out), reason])
    return excinfo.value


@pytest.mark.parametrize("reason", [NO_FREE_RU, NO_FREE_EN, NO_KEY_RU, NO_KEY_EN])
def test_license_refusal_is_a_separate_error_kind(tmp_path, fake_v8, reason):
    """Отказ в ВЫДАЧЕ лицензии обязан отличаться по типу от прочих отказов конфигуратора."""
    runner = DesignerRunner(v8_path=sys.executable, out_dir=tmp_path, timeout=60)

    error = _fail_with(runner, fake_v8, reason)

    assert isinstance(error, LicenseUnavailableError), (
        f"текст {reason!r} снят со стенда как отказ в выдаче лицензии, "
        f"а продукт вернул {type(error).__name__}: отличить временное от постоянного "
        f"по такому исключению нельзя"
    )
    assert isinstance(error, DesignerError), "тип обязан остаться совместимым с прежним"
    assert reason in str(error), "причина из /Out обязана остаться в тексте ошибки"


def test_storage_auth_failure_is_not_a_license_refusal(tmp_path, fake_v8):
    """Неверный пользователь хранилища ожиданием не лечится и повторяться не должен."""
    runner = DesignerRunner(v8_path=sys.executable, out_dir=tmp_path, timeout=60)

    error = _fail_with(runner, fake_v8, AUTH_RU)

    assert not isinstance(error, LicenseUnavailableError), (
        "ошибка аутентификации отнесена к временным — это вернуло бы бессмысленные повторы"
    )
    assert isinstance(error, DesignerError)


def _versions(count: int) -> list[StorageVersion]:
    return [
        StorageVersion(number=i, author="Автор", comment=f"Версия {i}",
                       date=dt.datetime(2026, 9, 18, 12, 0, 0) + dt.timedelta(minutes=i))
        for i in range(1, count + 1)
    ]


class LicenseFlakyBackend(FakeStorageBackend):
    """Первые ``refusals`` попыток версии отвечают отказом в выдаче лицензии."""

    def __init__(self, versions, refusals: dict[int, int], reason: str = NO_FREE_RU):
        super().__init__(versions)
        self.refusals = dict(refusals)
        self.reason = reason

    def export_version(self, version, dest, cancel=None):
        left = self.refusals.get(version, 0)
        if left > 0:
            self.refusals[version] = left - 1
            self.attempts[version] = self.attempts.get(version, 0) + 1
            raise LicenseUnavailableError(
                f"Конфигуратор завершился с кодом 1: ...\nСообщение конфигуратора:\n{self.reason}"
            )
        return super().export_version(version, dest, cancel)


@pytest.fixture()
def work_dir(tmp_path):
    path = tmp_path / "рабочая копия"
    path.mkdir()
    GitRepo(path).init()
    return path


def test_transient_license_refusal_is_retried_and_run_completes(work_dir, tmp_path, monkeypatch):
    """Настроенный retries обязан применяться к отказу в выдаче лицензии."""
    slept: list[float] = []
    monkeypatch.setattr("gitsync.sync.time.sleep", lambda s: slept.append(s))

    backend = LicenseFlakyBackend(_versions(3), refusals={2: 1})
    manager = SyncManager(work_dir, backend,
                          SyncOptions(jobs=1, retries=1, temp_root=tmp_path / "tmp"))

    result = manager.sync(raise_on_error=False)

    assert result.error is None, f"повтор не состоялся: {result.error}"
    assert result.committed == [1, 2, 3], (
        "одна несостоявшаяся выдача лицензии не должна стоить остальных версий"
    )
    assert backend.attempts[2] == 2, "версия 2 обязана быть переспрошена ровно один раз"
    assert slept, "между попытками обязана быть пауза: мгновенный повтор просит ту же лицензию"
    assert all(0 < s <= 120 for s in slept), f"пауза обязана быть ограниченной: {slept}"


def test_license_refusal_beyond_retries_still_fails_the_run(work_dir, tmp_path, monkeypatch):
    """Повтор ограничен: постоянная недоступность лицензии обязана оставаться отказом."""
    monkeypatch.setattr("gitsync.sync.time.sleep", lambda s: None)

    backend = LicenseFlakyBackend(_versions(2), refusals={1: 5})
    manager = SyncManager(work_dir, backend,
                          SyncOptions(jobs=1, retries=1, temp_root=tmp_path / "tmp"))

    result = manager.sync(raise_on_error=False)

    assert isinstance(result.error, LicenseUnavailableError)
    assert result.committed == []
    assert backend.attempts[1] == 2, "попыток ровно retries+1, без бесконечного ожидания"


def test_auth_failure_is_still_not_retried(work_dir, tmp_path, monkeypatch):
    """Граница правки: прочие отказы конфигуратора повторяться не начали."""
    monkeypatch.setattr("gitsync.sync.time.sleep", lambda s: None)

    backend = FakeStorageBackend(_versions(1),
                                 fail_versions={1: DesignerError("invalid credentials")})
    manager = SyncManager(work_dir, backend,
                          SyncOptions(jobs=1, retries=3, temp_root=tmp_path / "tmp"))

    result = manager.sync(raise_on_error=False)

    assert isinstance(result.error, DesignerError)
    assert not isinstance(result.error, LicenseUnavailableError)
    assert backend.attempts == {1: 1}, "повтор неверных учётных данных остаётся запрещённым"
