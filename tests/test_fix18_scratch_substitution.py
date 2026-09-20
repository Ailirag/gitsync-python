"""Fix18: очистка не имеет права уходить за пределы каталога, который создал этот запуск.

ВОСПРОИЗВЕДЕНИЕ ЗАМЕЧАНИЯ приёмки (fix17/eva-final/parent-link.log). `cleanup()` сначала
рекурсивно снимал рабочие каталоги по ЗАПОМНЕННЫМ ПУТЯМ и только потом проверял, не
подменён ли сам каталог запуска. Подмена КАТАЛОГА-ПРЕДКА уводила удаление в чужое дерево:
файл `external/<имя рабочего каталога>/foreign-sentinel.txt` исчезал, а предупреждение
«очистка отменена» появлялось уже ПОСЛЕ его удаления. Защита `shutil.rmtree` от ссылки
в самом удаляемом каталоге тут не работает: лист — настоящий каталог, подменён предок.

Прежняя проверка `test_scratch_replaced_by_link_is_not_followed` дефект пропускала:
в чужом дереве не было каталога с тем же именем, что у запомненного рабочего, и
удалять по подменённому пути было просто нечего.

Здесь проверяется ОДНО свойство: удаляется только тот объект файловой системы, который
этот запуск создал. Путь — не удостоверение (его можно подменить ссылкой, соединением
NTFS или другим настоящим каталогом), поэтому владение подтверждается удостоверением
каталога (устройство и inode), запомненным в момент создания. Потеря удостоверения —
отказ от очистки, а не попытка «угадать» своё дерево.

Модель угроз и остаточный TOCTOU описаны в docs/plugins.md; проверки ниже — только
наблюдаемое поведение: чужие файлы целы, свои сняты, отказ виден в журнале.
"""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from gitsync.backends import FixtureStorageBackend, NativeStorageBackend
from gitsync.designer import DesignerRunner, StorageAccess
from gitsync.plugins import PluginHost
from gitsync.sync import SyncManager, SyncOptions
from support.native_report import ReportVersion, build_report_mxl

SENTINEL = "ЧУЖОЙ ФАЙЛ — ОБЯЗАН УЦЕЛЕТЬ"


@pytest.fixture(autouse=True)
def _session_dir(monkeypatch, tmp_path):
    """Блокировки сессий хранилища — в tmp_path, а не в профиле запускающего."""
    monkeypatch.setenv("GITSYNC_SESSION_DIR", str(tmp_path / "sessions"))


@pytest.fixture()
def links():
    """Подмена пути ссылкой на чужой каталог и уборка ссылок за собой.

    На NTFS берётся соединение (junction): symlink требует особых прав, а соединение
    доступно всегда и остаётся точкой повторного разбора — ровно тем, по чему рекурсивное
    удаление и уходит в чужое дерево. На POSIX — обычная символьная ссылка.
    """
    created: list[Path] = []

    class Links:
        @staticmethod
        def make(link_path: Path, target: Path) -> None:
            target.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link_path), str(target)],
                               check=True, capture_output=True)
                info = link_path.lstat()
                assert info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            else:
                link_path.symlink_to(target, target_is_directory=True)
            created.append(link_path)

    yield Links()
    for path in created:
        try:
            os.rmdir(path) if os.name == "nt" else path.unlink()
        except OSError:
            pass


def _backend(root: Path, *, owns: bool = True) -> NativeStorageBackend:
    """Бэкенд без конфигуратора: ИБ подменена строкой, проверяется только владение каталогами."""
    return NativeStorageBackend(
        access=StorageAccess(path=str(root.parent / "storage"), user="probe"),
        runner=DesignerRunner("1cv8", root / "designer-out"),
        temp_root=root,
        owns_temp_root=owns,
        ib_factory=lambda worker_dir: "unused",
    )


def _started(root: Path, *, owns: bool = True) -> tuple[NativeStorageBackend, Path]:
    """Бэкенд, уже занявший каталог запуска и рабочий каталог потока."""
    backend = _backend(root, owns=owns)
    worker, _ = backend._worker_context()
    assert worker.is_dir() and worker.parent == root
    (worker / "своё.tmp").write_bytes(b"OWN")
    return backend, worker


def _foreign_worker(parent: Path, name: str) -> Path:
    """Чужой каталог, ИМЯ которого совпадает с запомненным рабочим каталогом."""
    foreign = parent / name
    foreign.mkdir(parents=True)
    sentinel = foreign / "foreign-sentinel.txt"
    sentinel.write_text(SENTINEL, encoding="utf-8")
    return sentinel


# --- подмена предка: точное воспроизведение приёмки ------------------------


def test_worker_is_not_deleted_through_substituted_scratch_root(tmp_path, links, caplog):
    """Каталог запуска подменён ссылкой на чужое дерево — рабочий каталог в нём не наш.

    Точный сценарий fix18/eva-parent-link-probe.py: свой каталог уезжает в сторону,
    на его месте оказывается ссылка на чужое дерево, а в чужом дереве лежит каталог
    С ТЕМ ЖЕ ИМЕНЕМ, что у запомненного рабочего.
    """
    root = tmp_path / "scratch"
    external = tmp_path / "external"
    backend, worker = _started(root)
    moved = tmp_path / "original-scratch"
    root.rename(moved)
    sentinel = _foreign_worker(external, worker.name)
    links.make(root, external)

    with caplog.at_level("WARNING"):
        backend.cleanup()

    assert sentinel.read_text(encoding="utf-8") == SENTINEL, (
        "очистка прошла по подменённому каталогу-предку и удалила чужой рабочий каталог"
    )
    assert (external / worker.name).is_dir(), "чужой каталог удалён целиком"
    assert (moved / worker.name / "своё.tmp").is_file(), "своё состояние уехало вместе с путём"
    assert any("очистка отменена" in message for message in caplog.messages), (
        "отказ от очистки обязан быть виден в журнале"
    )


def test_scratch_root_replaced_by_real_directory_is_not_deleted(tmp_path):
    """Подмена БЕЗ ссылок: на месте каталога запуска — другой настоящий каталог.

    Ссылок здесь нет вовсе, и проверка «это не ссылка» ничего не даёт: единственное,
    что отличает свой каталог от чужого, — удостоверение объекта файловой системы.
    """
    root = tmp_path / "tmp" / "native-real-swap"
    backend, worker = _started(root)
    moved = tmp_path / "tmp" / "уехавший-каталог"
    root.rename(moved)
    root.mkdir()
    sentinel = _foreign_worker(root, worker.name)

    backend.cleanup()

    assert sentinel.read_text(encoding="utf-8") == SENTINEL, (
        "удалён чужой каталог, оказавшийся на том же пути"
    )
    assert root.is_dir(), "удалён чужой каталог запуска целиком"
    assert (moved / worker.name / "своё.tmp").is_file()


def test_intermediate_ancestor_substitution_is_not_followed(tmp_path, links):
    """Подменён не сам каталог запуска, а каталог НАД ним."""
    base = tmp_path / "общий корень"
    root = base / "native-mid"
    backend, worker = _started(root)
    base.rename(tmp_path / "общий корень-original")
    foreign = tmp_path / "чужое дерево"
    sentinel = _foreign_worker(foreign / root.name, worker.name)
    links.make(base, foreign)

    backend.cleanup()

    assert sentinel.read_text(encoding="utf-8") == SENTINEL, (
        "очистка прошла по подменённому промежуточному каталогу"
    )
    assert (foreign / root.name).is_dir()


def test_external_root_worker_is_not_deleted_through_substituted_path(tmp_path, links):
    """Внешний корень (`owns_temp_root=False`): свои рабочие каталоги тоже по удостоверению.

    Внешний корень не удаляют никогда, но рабочие каталоги в нём принадлежат запуску —
    и снимать их по пути, который увёл в чужое дерево, так же нельзя.
    """
    external_root = tmp_path / "постоянные данные"
    external_root.mkdir()
    (external_root / "cache.bin").write_bytes(b"PERSISTENT")
    backend, worker = _started(external_root, owns=False)
    external_root.rename(tmp_path / "переехавший корень")
    foreign = tmp_path / "чужое дерево"
    sentinel = _foreign_worker(foreign, worker.name)
    links.make(external_root, foreign)

    backend.cleanup()

    assert sentinel.read_text(encoding="utf-8") == SENTINEL
    assert (tmp_path / "переехавший корень" / "cache.bin").read_bytes() == b"PERSISTENT"


def test_worker_replaced_by_link_is_not_followed(tmp_path, links):
    """Подменён сам рабочий каталог: снимается ссылка, а не дерево за ней."""
    root = tmp_path / "tmp" / "native-leaf"
    backend, worker = _started(root)
    for item in worker.iterdir():
        item.unlink()
    worker.rmdir()
    foreign = tmp_path / "чужое дерево"
    sentinel = _foreign_worker(foreign, "вложенный")
    links.make(worker, foreign)

    backend.cleanup()

    assert sentinel.read_text(encoding="utf-8") == SENTINEL, "удаление ушло по ссылке рабочего каталога"
    assert foreign.is_dir()


# --- очистка своего по-прежнему работает -----------------------------------


def test_intact_run_is_removed_completely(tmp_path):
    """Неподменённый прогон снимается целиком: отказ от очистки — не «всегда отказ»."""
    root = tmp_path / "tmp" / "native-intact"
    backend, worker = _started(root)
    (worker / "ib").mkdir()
    (worker / "ib" / "1Cv8.1CD").write_bytes(b"\x00" * 1024)
    (root / "designer-out").mkdir(parents=True, exist_ok=True)
    (root / "designer-out" / "run.log").write_text("протокол", encoding="utf-8")

    backend.cleanup()

    assert not root.exists(), "свой каталог запуска обязан сниматься полностью"
    assert (tmp_path / "tmp").is_dir(), "общий родитель запуску не принадлежит"


def test_second_worker_of_the_same_run_is_removed(tmp_path):
    """Несколько потоков — несколько рабочих каталогов, и снимаются все."""
    root = tmp_path / "tmp" / "native-two-workers"
    backend, first = _started(root)
    second: list[Path] = []
    thread = threading.Thread(target=lambda: second.append(backend._worker_context()[0]))
    thread.start()
    thread.join(timeout=30)
    assert second and second[0] != first

    backend.cleanup()

    assert not root.exists()


def test_failure_keeps_the_root_but_removes_workers(tmp_path):
    """Диагностика отказа остаётся, одноразовое состояние снимается — порядок не изменился."""
    root = tmp_path / "tmp" / "native-failed"
    backend, worker = _started(root)
    (root / "designer-out").mkdir(parents=True, exist_ok=True)
    kept = root / "designer-out" / "designer.log"
    kept.write_text("причина отказа", encoding="utf-8")
    # Флаг ставит декоратор _keeps_diagnostics_on_failure на любом отказе конфигуратора.
    backend._keep_diagnostics = True

    backend.cleanup()

    assert kept.read_text(encoding="utf-8") == "причина отказа"
    assert not worker.exists(), "временная ИБ диагностикой не является"


def test_cleanup_after_refusal_is_repeatable(tmp_path, links):
    """Повторная очистка после отказа не падает и чужого не трогает."""
    root = tmp_path / "scratch"
    external = tmp_path / "external"
    backend, worker = _started(root)
    root.rename(tmp_path / "original-scratch")
    sentinel = _foreign_worker(external, worker.name)
    links.make(root, external)

    backend.cleanup()
    backend.cleanup()

    assert sentinel.read_text(encoding="utf-8") == SENTINEL


def test_cleanup_of_already_removed_run_is_silent(tmp_path, caplog):
    """Каталога уже нет (том очистили снаружи): это не подмена и не повод для тревоги."""
    root = tmp_path / "tmp" / "native-gone"
    backend, _worker = _started(root)
    import shutil as _shutil

    _shutil.rmtree(root)

    with caplog.at_level("WARNING"):
        backend.cleanup()

    assert not [message for message in caplog.messages if "отменена" in message], (
        f"исчезнувший каталог принят за подмену: {caplog.messages}"
    )


# --- каталог выгрузки прогона ----------------------------------------------


def test_export_run_root_is_not_removed_through_substituted_parent(tmp_path, links):
    """Тот же класс дефекта в очистке каталога выгрузки `run-<uuid>`, а не только у бэкенда.

    Каталог выгрузки создаёт сам прогон (`exist_ok=False`) в родителе, который задал
    оператор, и снимает его в защищённом `finally`. Подмена родителя между работой и
    очисткой уводила рекурсивное удаление в чужое дерево ровно так же, как у каталога
    запуска бэкенда. Подмена делается из обработчика `after_sync`: работа закончена,
    очистка ещё впереди — то самое окно.

    Несостоявшаяся очистка остаётся ОШИБКОЙ результата (контракт R07: отказ снятия
    собственных ресурсов виден вызывающему), поэтому проверяются оба следствия:
    чужое дерево цело и отказ не проглочен.
    """
    storage = tmp_path / "фикстура хранилища"
    (storage / "v1" / "Справочники").mkdir(parents=True)
    (storage / "v1" / "Справочники" / "Товары.xml").write_text("<Товары/>", encoding="utf-8")
    (storage / "report.mxl").write_bytes(
        build_report_mxl([ReportVersion(1, "Иванов", "15.09.2026", "10:20:30", "Первая версия")])
    )
    parent = tmp_path / "tmp"
    away = tmp_path / "чужое дерево"
    substituted: dict[str, Path] = {}

    def swap_parent(context) -> None:
        run_root = next(parent.glob("run-*"))
        substituted["sentinel"] = _foreign_worker(away / run_root.name, "вложенный")
        parent.rename(tmp_path / "tmp-original")
        links.make(parent, away)

    host = PluginHost()
    host.subscribe("after_sync", swap_parent, contextual=True)
    manager = SyncManager(
        tmp_path / "рабочая копия",
        FixtureStorageBackend(storage),
        SyncOptions(jobs=1, temp_root=str(parent), lock_timeout=5),
        plugins=host,
    )

    result = manager.sync(raise_on_error=False)

    sentinel = substituted["sentinel"]
    assert sentinel.read_text(encoding="utf-8") == SENTINEL, (
        "очистка каталога выгрузки прошла по подменённому родителю"
    )
    assert result.committed == [1], "версия обязана быть зафиксирована: очистка идёт после работы"
    assert result.error is not None and "Каталог выгрузки" in str(result.error), (
        f"несостоявшаяся очистка обязана быть видна вызывающему: {result.error!r}"
    )


# --- первое обращение из нескольких потоков --------------------------------


def test_first_use_from_several_threads_creates_the_root_once(tmp_path, monkeypatch):
    """Гонка первого создания каталога запуска (P3-1 рецензии fix17).

    Проверка `_scratch_created` и `mkdir(exist_ok=False)` — две операции; между ними
    успевают пройти остальные потоки, и на образе fix17 это давало 3 `FileExistsError`
    из 4 потоков. Гонка не «ловится повторами»: настоящий `mkdir` задерживается ровно
    на первом вызове, поэтому окно между проверкой и созданием открыто детерминированно.
    """
    root = tmp_path / "tmp" / "native-race"
    backend = _backend(root)
    workers = 4
    at_once = threading.Barrier(workers)
    first_call = threading.Event()
    real_mkdir = Path.mkdir

    def slow_mkdir(self, *args, **kwargs):
        if self == root and not first_call.is_set():
            first_call.set()
            time.sleep(0.3)
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", slow_mkdir)
    failures: list[BaseException] = []
    created: list[Path] = []

    def worker() -> None:
        at_once.wait(timeout=30)
        try:
            created.append(backend._worker_context()[0])
        except BaseException as exc:  # noqa: BLE001 — гонка проявляется любым исключением
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not failures, f"первое обращение из нескольких потоков отказало: {failures!r}"
    assert len(created) == workers
    assert len(set(created)) == workers, "рабочие каталоги потоков обязаны быть разными"
    assert {path.parent for path in created} == {root}

    backend.cleanup()
    assert not root.exists()
