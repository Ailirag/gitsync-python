"""Stage-14: очистка не имеет права уничтожать чужое, признав его чужим.

ВОСПРОИЗВЕДЕНИЕ ЗАМЕЧАНИЙ приёмки (P1 EVA-2a: S4b и S6; EVA-2b).

S4b. Рабочий каталог подменяли, снятие отказывало по удостоверению — и тут же
рекурсивная уборка каталога ЗАПУСКА уносила подменённое содержимое. «Это не наше» и
«значит, удалим вместе с родителем» — противоречие; подброшенный каталог исчезал
(``planted_content_alive=false``).

S6. Финальное снятие шло по ИМЕНИ: между сверкой записи и ``rmdir`` запись подменяли
чужим ПУСТЫМ каталогом, и снимался он (``foreign_empty_dir_alive=false``).

EVA-2b. Пропавшее имя считалось успешной очисткой. После переименования свой каталог
оставался жив, а прогон сообщал, что убрал его.

Здесь проверяется только наблюдаемое поведение: чужое цело, своё снято, отказ виден.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gitsync import safepath
from gitsync.backends import NativeStorageBackend
from gitsync.designer import DesignerRunner, StorageAccess
from gitsync.safepath import directory_identity, discard_owned_directory

FOREIGN = "ЧУЖОЙ ФАЙЛ — ОБЯЗАН УЦЕЛЕТЬ"
IS_WIN = os.name == "nt"


@pytest.fixture(autouse=True)
def _session_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GITSYNC_SESSION_DIR", str(tmp_path / "sessions"))


def _backend(root: Path, *, owns: bool = True) -> NativeStorageBackend:
    return NativeStorageBackend(
        access=StorageAccess(path=str(root.parent / "storage"), user="probe"),
        runner=DesignerRunner("1cv8", root / "designer-out"),
        temp_root=root,
        owns_temp_root=owns,
        ib_factory=lambda worker_dir: "unused",
    )


def _plant(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "foreign-sentinel.txt"
    marker.write_text(FOREIGN, encoding="utf-8")
    return marker


# --- S4b: отказ по удостоверению НЕ разрешает снести содержимое вместе с родителем ---

def test_s4b_refused_worker_content_survives_scratch_root_cleanup(tmp_path):
    """Подменённый рабочий каталог не снимается ни сам, ни заодно с каталогом запуска."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()

    worker.rename(tmp_path / "moved-worker")
    planted = _plant(worker / "victim")

    backend.cleanup()

    assert planted.is_file(), "подброшенное содержимое снято вместе с каталогом запуска"
    assert planted.read_text(encoding="utf-8") == FOREIGN
    assert root.is_dir(), "каталог запуска обязан остаться: внутри отклонённое содержимое"
    assert backend._worker_dirs, "отклонённый каталог обязан остаться на учёте"


def test_s4b_refusal_is_visible_in_log(tmp_path, caplog):
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    worker.rename(tmp_path / "moved-worker")
    _plant(worker / "victim")

    with caplog.at_level("WARNING"):
        backend.cleanup()

    messages = "\n".join(caplog.messages)
    assert "перестал быть своим" in messages
    assert "отклонённое" in messages or "отклонена" in messages, messages


def test_s4b_other_content_of_own_root_is_still_removed(tmp_path):
    """Защита касается ТОЛЬКО отклонённого имени: остальное убирается как прежде."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    (root / "designer-out").mkdir(parents=True, exist_ok=True)
    (root / "designer-out" / "out.log").write_text("протокол", encoding="utf-8")
    ordinary = root / "ordinary-temp"
    ordinary.mkdir()
    (ordinary / "junk.tmp").write_bytes(b"x")

    worker.rename(tmp_path / "moved-worker")
    planted = _plant(worker / "victim")

    backend.cleanup()

    assert planted.is_file()
    assert not ordinary.exists(), "обычное одноразовое состояние обязано сниматься"
    assert not (root / "designer-out").exists()


# --- S6: финальное снятие идёт по изолированному объекту, а не по имени --------------

@pytest.mark.skipif(IS_WIN, reason="финальный rmdir по имени — примитив POSIX")
def test_s6_foreign_empty_directory_swapped_before_final_rmdir_survives(tmp_path, monkeypatch):
    """Чужой ПУСТОЙ каталог, подставленный перед снятием, обязан уцелеть."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    backend._worker_context()

    real_stat = safepath.os.stat
    state = {"armed": True, "fired": False}

    def hooked(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if state["armed"] and kwargs.get("dir_fd") and path == root.name:
            import traceback
            if "_rmdir_emptied_posix" in [f.name for f in traceback.extract_stack()]:
                state["armed"] = False
                state["fired"] = True
                root.rename(tmp_path / "moved")
                root.mkdir()          # чужой ПУСТОЙ каталог ровно на этом пути
        return result

    monkeypatch.setattr(safepath.os, "stat", hooked)
    backend.cleanup()

    assert state["fired"], "подмена не сработала: проба ничего не проверила"
    assert root.is_dir(), "чужой пустой каталог снят финальным rmdir"


@pytest.mark.skipif(IS_WIN, reason="финальный rmdir по имени — примитив POSIX")
def test_s6_foreign_non_empty_directory_swapped_before_final_rmdir_survives(tmp_path, monkeypatch):
    """То же с НЕпустым чужим каталогом: он и подавно не должен пострадать."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    backend._worker_context()
    state = {"armed": True, "fired": False}
    real_stat = safepath.os.stat
    keep: dict[str, Path] = {}

    def hooked(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if state["armed"] and kwargs.get("dir_fd") and path == root.name:
            import traceback
            if "_rmdir_emptied_posix" in [f.name for f in traceback.extract_stack()]:
                state["armed"] = False
                state["fired"] = True
                root.rename(tmp_path / "moved")
                root.mkdir()
                keep["marker"] = _plant(root / "victim")
        return result

    monkeypatch.setattr(safepath.os, "stat", hooked)
    backend.cleanup()

    assert state["fired"]
    assert keep["marker"].is_file(), "чужое содержимое уничтожено финальным снятием"
    assert keep["marker"].read_text(encoding="utf-8") == FOREIGN


@pytest.mark.skipif(IS_WIN, reason="финальный rmdir по имени — примитив POSIX")
def test_s6_isolation_leaves_no_temporary_name_behind(tmp_path, monkeypatch):
    """После отказа чужой объект возвращается на своё имя, временного не остаётся."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    backend._worker_context()
    real_stat = safepath.os.stat
    state = {"armed": True}

    def hooked(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if state["armed"] and kwargs.get("dir_fd") and path == root.name:
            import traceback
            if "_rmdir_emptied_posix" in [f.name for f in traceback.extract_stack()]:
                state["armed"] = False
                root.rename(tmp_path / "moved")
                root.mkdir()
        return result

    monkeypatch.setattr(safepath.os, "stat", hooked)
    backend.cleanup()

    leftovers = [item.name for item in tmp_path.iterdir()
                 if item.name.startswith(".gitsync-discard-")]
    assert not leftovers, f"остался временный изолятор: {leftovers}"


# --- EVA-2b: пропавшее имя — не снятый каталог --------------------------------------

@pytest.mark.skipif(IS_WIN, reason="проверка числа ссылок у открытого каталога — POSIX")
def test_eva2b_renamed_own_directory_is_not_reported_as_cleaned(tmp_path, monkeypatch):
    """Свой каталог переименовали: он жив, и очистка обязана сообщить об отказе."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    backend._worker_context()
    real_stat = safepath.os.stat
    state = {"armed": True, "fired": False}

    def hooked(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if state["armed"] and kwargs.get("dir_fd") and path == root.name:
            import traceback
            if "_rmdir_emptied_posix" in [f.name for f in traceback.extract_stack()]:
                state["armed"] = False
                state["fired"] = True
                root.rename(tmp_path / "moved")   # имя исчезло, каталог ЖИВ
        return result

    monkeypatch.setattr(safepath.os, "stat", hooked)
    backend.cleanup()

    assert state["fired"]
    moved = tmp_path / "moved"
    assert moved.is_dir(), "свой каталог должен был остаться (его переименовали)"
    assert backend._scratch_created, (
        "очистка объявлена состоявшейся, хотя каталог жив под другим именем"
    )


@pytest.mark.skipif(IS_WIN, reason="проверка числа ссылок у открытого каталога — POSIX")
def test_eva2b_truly_removed_directory_is_still_a_success(tmp_path, monkeypatch):
    """Контроль: если каталог ДЕЙСТВИТЕЛЬНО снят, очистка обязана сообщить об успехе."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    backend._worker_context()
    real_stat = safepath.os.stat
    state = {"armed": True, "fired": False}

    def hooked(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if state["armed"] and kwargs.get("dir_fd") and path == root.name:
            import traceback
            if "_rmdir_emptied_posix" in [f.name for f in traceback.extract_stack()]:
                state["armed"] = False
                state["fired"] = True
                os.rmdir(root)        # снят по-настоящему, не переименован
        return result

    monkeypatch.setattr(safepath.os, "stat", hooked)
    backend.cleanup()

    assert state["fired"]
    assert not root.exists()
    assert not backend._scratch_created, "настоящее снятие обязано считаться успехом"


# --- обычная работа не должна пострадать --------------------------------------------

def test_normal_cleanup_still_removes_everything(tmp_path):
    """Защита не выродилась в «ничего не удаляем»."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    (worker / "ib").mkdir()
    (worker / "ib" / "1Cv8.1CD").write_bytes(b"x" * 16)
    (worker / "deep" / "deeper").mkdir(parents=True)
    (worker / "deep" / "deeper" / "file.xml").write_text("<x/>", encoding="utf-8")
    neighbour = _plant(tmp_path / "neighbour")

    backend.cleanup()

    assert not root.exists(), "обычная очистка обязана снять каталог запуска целиком"
    assert not backend._worker_dirs
    assert neighbour.is_file(), "сосед снаружи не должен быть тронут"


def test_repeated_cleanup_is_silent(tmp_path, caplog):
    root = tmp_path / "scratch"
    backend = _backend(root)
    backend._worker_context()
    backend.cleanup()
    with caplog.at_level("WARNING"):
        backend.cleanup()
        backend.cleanup()
    assert not [m for m in caplog.messages if "отменена" in m], caplog.messages


def test_keep_temp_keeps_diagnostics_and_does_not_refuse(tmp_path):
    """Удержание диагностики — не отказ: каталог остаётся намеренно и молча."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    backend._keep_diagnostics = True
    (root / "designer-out").mkdir(parents=True, exist_ok=True)
    (root / "designer-out" / "out.log").write_text("протокол", encoding="utf-8")

    backend.cleanup()

    assert root.is_dir()
    assert (root / "designer-out" / "out.log").is_file()
    assert not worker.exists(), "рабочий каталог снимается даже при удержании диагностики"


def test_external_root_is_never_removed(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    keep = _plant(external / "чужое")
    backend = _backend(external, owns=False)
    worker, _ = backend._worker_context()

    backend.cleanup()

    assert external.is_dir(), "внешний корень не удаляется никогда"
    assert keep.is_file()
    assert not worker.exists(), "свой рабочий каталог внутри внешнего корня снимается"


# --- прямой контракт discard_owned_directory ----------------------------------------

def test_protect_keeps_named_entry_and_refuses_the_parent(tmp_path):
    owned = tmp_path / "owned"
    (owned / "keep-me").mkdir(parents=True)
    keep = _plant(owned / "keep-me")
    (owned / "drop-me").mkdir()
    (owned / "drop-me" / "junk").write_bytes(b"x")
    identity = directory_identity(owned)

    removed = discard_owned_directory(owned, identity, "Каталог", {"keep-me"})

    assert removed is False, "с защищённой записью снятие каталога не может состояться"
    assert keep.is_file(), "защищённая запись уничтожена"
    assert not (owned / "drop-me").exists(), "незащищённое обязано сниматься"


def test_protect_default_keeps_previous_behaviour(tmp_path):
    owned = tmp_path / "owned"
    (owned / "sub").mkdir(parents=True)
    (owned / "sub" / "file").write_bytes(b"x")
    identity = directory_identity(owned)

    assert discard_owned_directory(owned, identity, "Каталог") is True
    assert not owned.exists()
