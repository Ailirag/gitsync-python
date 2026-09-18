"""Fix19: удаление идёт по ЗАКРЕПЛЁННОМУ объекту, а не по пути.

ВОСПРОИЗВЕДЕНИЕ ЗАМЕЧАНИЯ приёмки. В fix18 удостоверение каталога сверялось
непосредственно перед ``shutil.rmtree`` по пути, и на Windows между сверкой и удалением
оставалось окно: подмена НАСТОЯЩИМ каталогом в этот момент уничтожала чужое дерево. В
пробе рецензента (``fix18/review/evidence/probes-windows.json``, D1) это записано как
``foreign_alive=false, expected_alive=false, ok=true`` — потеря чужих данных была
засчитана как ожидаемое ограничение. Здесь она проверяется утверждением, которое
выполняется ТОЛЬКО при уцелевшем чужом файле.

Второе замечание того же класса: каталог версии ``v<N>-<hex>`` внутри каталога выгрузки
снимался обычным ``rmtree`` по пути (``sync.py``), и подмена ПРЕДКА уводила рекурсивное
удаление в чужое дерево — на обеих платформах.

Закрепление: в POSIX — дескриптор каталога (``O_DIRECTORY | O_NOFOLLOW``), на Windows —
описатель без ``FILE_SHARE_DELETE``, который запрещает переименование и самого каталога,
и его предков, а снятие идёт по описателю. Модель угроз — docs/plugins.md.
"""

from __future__ import annotations

import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from gitsync import safepath
from gitsync.backends import FixtureStorageBackend, NativeStorageBackend
from gitsync.designer import DesignerRunner, StorageAccess
from gitsync.errors import GitSyncError
from gitsync.plugins import PluginHost
from gitsync.safepath import directory_identity, discard_owned_directory
from gitsync.sync import SyncManager, SyncOptions
from support.native_report import ReportVersion, build_report_mxl

SENTINEL = "ЧУЖОЙ ФАЙЛ — ОБЯЗАН УЦЕЛЕТЬ"
IS_WIN = os.name == "nt"


@pytest.fixture(autouse=True)
def _session_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GITSYNC_SESSION_DIR", str(tmp_path / "sessions"))


def _link(link_path: Path, target: Path) -> None:
    """Соединение NTFS на Windows, символьная ссылка в POSIX — обе ведут наружу."""
    target.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link_path), str(target)],
                       check=True, capture_output=True)
        assert link_path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    else:
        link_path.symlink_to(target, target_is_directory=True)


def _backend(root: Path, *, owns: bool = True) -> NativeStorageBackend:
    return NativeStorageBackend(
        access=StorageAccess(path=str(root.parent / "storage"), user="probe"),
        runner=DesignerRunner("1cv8", root / "designer-out"),
        temp_root=root,
        owns_temp_root=owns,
        ib_factory=lambda worker_dir: "unused",
    )


def _sentinel(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "foreign-sentinel.txt"
    path.write_text(SENTINEL, encoding="utf-8")
    return path


def _swap_after(monkeypatch, hook: str, swap, *, match, caller: str) -> dict:
    """Подменяет объект ОДИН раз в названной точке пакета и запоминает момент.

    ``caller`` не даёт сработать в случайном месте: проверяется именно окно между
    проверкой удостоверения и удалением, а не любой вызов ``lstat`` в процессе.
    """
    real = getattr(safepath.os, hook)
    state: dict = {"armed": True, "fired": False}

    def hooked(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if state["armed"] and match(path, kwargs):
            import traceback
            if caller in [frame.name for frame in traceback.extract_stack()]:
                state["armed"] = False
                state["fired"] = True
                swap()
        return result

    monkeypatch.setattr(safepath.os, hook, hooked)
    return state


def _by_path(target: Path):
    return lambda path, kwargs: not kwargs.get("dir_fd") and str(path) == str(target)


# --- окно между проверкой удостоверения и удалением --------------------------------

@pytest.mark.parametrize("kind", ["real-directory", "link"])
def test_substitution_right_after_the_identity_check_keeps_foreign_data(tmp_path, monkeypatch, kind):
    """Каталог запуска подменяют ПОСЛЕ проверки: чужой файл обязан уцелеть.

    ``real-directory`` — подмена настоящим каталогом (ни ссылки, ни соединения: проверка
    «это не ссылка» здесь бессильна, работает только удостоверение). ``link`` — ссылка
    наружу. До fix19 первый случай на Windows уничтожал чужое дерево.
    """
    root = tmp_path / "scratch"
    backend = _backend(root)
    backend._worker_context()
    external = tmp_path / "external"
    outside = _sentinel(external / "victim")

    def swap() -> None:
        root.rename(tmp_path / "moved")
        if kind == "real-directory":
            root.mkdir()
            _sentinel(root / "victim")
        else:
            _link(root, external)

    state = _swap_after(monkeypatch, "lstat", swap, match=_by_path(root),
                        caller="discard_owned_directory")
    backend.cleanup()

    assert state["fired"], "подмена не сработала: проба ничего не проверила"
    survivor = (root / "victim" / "foreign-sentinel.txt") if kind == "real-directory" else outside
    assert survivor.is_file(), "очистка уничтожила чужие данные после подмены пути"
    assert survivor.read_text(encoding="utf-8") == SENTINEL


@pytest.mark.parametrize("kind", ["real-directory", "link"])
def test_worker_substitution_right_after_the_identity_check_is_refused(tmp_path, monkeypatch, kind):
    """То же окно у РАБОЧЕГО каталога: снятие отказывает по удостоверению."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    external = tmp_path / "external"
    outside = _sentinel(external / "victim")

    def swap() -> None:
        worker.rename(tmp_path / "moved-worker")
        if kind == "real-directory":
            worker.mkdir()
        else:
            _link(worker, external)

    state = _swap_after(monkeypatch, "lstat", swap, match=_by_path(worker),
                        caller="discard_owned_directory")
    backend.cleanup()

    assert state["fired"]
    assert outside.is_file(), "удаление ушло по ссылке в чужое дерево"
    assert backend._worker_dirs, (
        "отказ обязан оставить каталог на учёте: иначе повторная очистка о нём не узнает"
    )


@pytest.mark.skipif(not IS_WIN, reason="описатель без FILE_SHARE_DELETE — примитив Windows")
def test_windows_pin_makes_the_substitution_itself_impossible(tmp_path, monkeypatch):
    """После закрепления подмену отвергает САМА система, а не наша проверка.

    Это и есть разница с fix18: там окно закрывалось «успеть проверить», здесь его нет —
    пока описатель открыт, каталог не переименовать (``ERROR_SHARING_VIOLATION``).
    """
    root = tmp_path / "scratch"
    backend = _backend(root)
    backend._worker_context()
    outcome: dict = {}

    def swap() -> None:
        try:
            root.rename(tmp_path / "moved")
        except OSError as exc:
            outcome["winerror"] = exc.winerror
            return
        outcome["winerror"] = None

    state = _swap_after(monkeypatch, "scandir", swap, match=_by_path(root),
                        caller="_empty_pinned_windows")
    backend.cleanup()

    assert state["fired"]
    assert outcome["winerror"] == 32, "закреплённый каталог удалось переименовать"
    assert not root.exists(), "свой каталог всё-таки должен быть снят"


@pytest.mark.skipif(IS_WIN, reason="dir_fd-удаление — примитив POSIX")
def test_posix_emptied_directory_with_a_foreign_entry_is_not_reported_as_removed(
        tmp_path, monkeypatch):
    """Подмена ПОСЛЕ закрепления: чужое цело, а свой каталог не выдаётся за снятый.

    В fix18 финальный ``rmdir`` шёл по пути, ловил ``FileNotFoundError`` и возвращал
    «снято»: опустошённый свой каталог молча оставался на диске.
    """
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    (worker / "своё.tmp").write_bytes(b"OWN")
    external = tmp_path / "external"
    outside = _sentinel(external / "victim")

    def swap() -> None:
        root.rename(tmp_path / "moved")
        _link(root, external)

    state = _swap_after(monkeypatch, "listdir", swap,
                        match=lambda path, kwargs: isinstance(path, int),
                        caller="_empty_pinned_directory")
    backend.cleanup()

    assert state["fired"]
    assert outside.is_file(), "удаление ушло по подменённому пути наружу"
    moved = tmp_path / "moved" / worker.name
    assert not (moved / "своё.tmp").exists(), "содержимое своего каталога снято по дескриптору"
    assert moved.is_dir(), "проба не воспроизвела остаток: каталог всё-таки снят"
    assert backend._worker_dirs, (
        "оставшийся свой каталог обязан остаться на учёте, а не считаться снятым"
    )


# --- каталог версии внутри каталога выгрузки ---------------------------------------

def _fixture_storage(root: Path) -> Path:
    (root / "v1" / "Справочники").mkdir(parents=True)
    (root / "v1" / "Справочники" / "Товары.xml").write_text("<Товары/>", encoding="utf-8")
    (root / "report.mxl").write_bytes(build_report_mxl([
        ReportVersion(1, "Иванов", "15.09.2026", "10:20:30", "Первая версия"),
    ]))
    return root


def _substitute_run_root(tmp_path: Path, parent: Path, seen: dict):
    """Каталог выгрузки уводят ссылкой на чужое дерево ТОЙ ЖЕ раскладки."""
    def swap(context=None) -> None:
        if seen:
            return
        run_root = next(parent.glob("run-*"))
        version_dir = next(run_root.glob("v*-*"))
        foreign = tmp_path / "чужое дерево" / run_root.name / version_dir.name
        foreign.mkdir(parents=True)
        seen["sentinel"] = _sentinel(foreign)
        (foreign / "вложенный").mkdir()
        seen["nested"] = foreign / "вложенный" / "тоже чужой.txt"
        seen["nested"].write_text(SENTINEL, encoding="utf-8")
        run_root.rename(tmp_path / "run-original")
        _link(run_root, tmp_path / "чужое дерево" / run_root.name)
    return swap


def test_version_directory_is_not_removed_through_a_substituted_parent(tmp_path):
    """Успешный коммит: каталог версии снимается по удостоверению, а не по пути."""
    parent = tmp_path / "общий temp"
    seen: dict = {}
    host = PluginHost()
    host.subscribe("before_commit", _substitute_run_root(tmp_path, parent, seen), contextual=True)
    manager = SyncManager(tmp_path / "работа",
                          FixtureStorageBackend(_fixture_storage(tmp_path / "фикстура")),
                          SyncOptions(jobs=1, temp_root=str(parent), lock_timeout=5), plugins=host)
    manager.sync(raise_on_error=False)

    assert seen, "подмена не сработала: проба ничего не проверила"
    assert seen["sentinel"].is_file(), "удаление каталога версии ушло в чужое дерево"
    assert seen["nested"].is_file(), "чужое поддерево уничтожено"


def test_failed_export_does_not_remove_the_version_directory_through_a_substituted_parent(tmp_path):
    """Тот же путь удаления на ОТКАЗЕ выгрузки: ``_export_one`` снимал каталог по пути."""
    parent = tmp_path / "общий temp"
    seen: dict = {}
    swap = _substitute_run_root(tmp_path, parent, seen)

    class BreaksAfterExport(FixtureStorageBackend):
        def export_version(self, version, dest, cancel=None):
            super().export_version(version, dest, cancel)
            swap()
            raise GitSyncError("сбой выгрузки после создания каталога версии")

    manager = SyncManager(tmp_path / "работа",
                          BreaksAfterExport(_fixture_storage(tmp_path / "фикстура")),
                          SyncOptions(jobs=1, retries=0, temp_root=str(parent), lock_timeout=5))
    result = manager.sync(raise_on_error=False)

    assert seen, "подмена не сработала: проба ничего не проверила"
    assert result.error is not None
    assert seen["sentinel"].is_file(), "удаление каталога версии ушло в чужое дерево"
    assert seen["nested"].is_file(), "чужое поддерево уничтожено"


def test_refused_cleanup_after_substitution_reaches_the_result(tmp_path):
    """Несостоявшаяся очистка СВОИХ каталогов обязана стать ошибкой результата.

    ``ignore_errors=True`` гасил след: каталог версии оставался, а прогон объявлял успех.
    Здесь подменён предок, поэтому отказывают обе очистки — и отказ виден вызывающему,
    а не только в журнале (контракт R07).
    """
    parent = tmp_path / "общий temp"
    seen: dict = {}
    host = PluginHost()
    host.subscribe("before_commit", _substitute_run_root(tmp_path, parent, seen), contextual=True)
    manager = SyncManager(tmp_path / "работа",
                          FixtureStorageBackend(_fixture_storage(tmp_path / "фикстура")),
                          SyncOptions(jobs=1, temp_root=str(parent), lock_timeout=5), plugins=host)
    result = manager.sync(raise_on_error=False)

    assert seen, "подмена не сработала: проба ничего не проверила"
    assert result.error is not None, "отказ очистки не дошёл до результата"
    assert "снять не удалось" in str(result.error)
    assert seen["sentinel"].is_file()


def test_leftover_own_directory_is_reported_not_silenced(tmp_path):
    """Оставшийся собственный каталог — ошибка, отсутствующий — тишина."""
    manager = SyncManager(tmp_path / "работа", None, SyncOptions())
    present = tmp_path / "остался"
    present.mkdir()
    with pytest.raises(GitSyncError, match="Каталог версии"):
        manager._require_discarded(present, "Каталог версии")
    manager._require_discarded(tmp_path / "нет такого", "Каталог версии")


# --- удостоверение и учёт ----------------------------------------------------------

def test_zero_inode_is_not_an_identity(tmp_path, monkeypatch):
    """``st_ino == 0`` сделал бы разные каталоги одинаковыми: это отсутствие удостоверения."""
    target = tmp_path / "dir"
    target.mkdir()
    assert directory_identity(target) is not None

    real = safepath.os.lstat

    class Zeroed:
        """``stat`` тома, не отдающего индекс: всё как у настоящего, но ``st_ino == 0``."""

        def __init__(self, info):
            self._info = info
            self.st_ino = 0

        def __getattr__(self, name):
            return getattr(self._info, name)

    def zeroed(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        return Zeroed(info) if str(path) == str(target) else info

    monkeypatch.setattr(safepath.os, "lstat", zeroed)
    assert directory_identity(target) is None
    assert discard_owned_directory(target, (1, 2), "Каталог") is False
    assert target.is_dir(), "каталог без удостоверения снимать нельзя"


def test_cleanup_and_first_use_do_not_interleave(tmp_path):
    """Очистка не имеет права снять каталог и оставить о нём запись.

    Раньше ``cleanup()``, начатая одновременно с первой работой другого потока, снимала
    его рабочий каталог и оставляла его в списке: учёт расходился с диском.
    """
    root = tmp_path / "scratch"
    backend = _backend(root)
    backend._worker_context()
    started = threading.Barrier(2)
    errors: list[BaseException] = []

    def late_worker() -> None:
        try:
            started.wait(timeout=10)
            worker, _ = backend._worker_context()
            assert worker.is_dir()
        except BaseException as exc:  # noqa: BLE001 — переносим в главный поток
            errors.append(exc)

    def cleaner() -> None:
        try:
            started.wait(timeout=10)
            backend.cleanup()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=late_worker), threading.Thread(target=cleaner)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    for worker_dir, _ in backend._worker_dirs:
        assert worker_dir.is_dir(), (
            f"каталог снят, но остался на учёте: {worker_dir}"
        )
