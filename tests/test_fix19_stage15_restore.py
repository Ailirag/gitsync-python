"""Stage-15: возврат из изоляции не имеет права замещать чужой объект (P1 EVA-13).

ВОСПРОИЗВЕДЕНИЕ ЗАМЕЧАНИЯ приёмки. В stage-14 возврат делался обычным ``os.rename``.
В POSIX ``rename`` для каталогов ЗАМЕЩАЕТ пустую цель молча. Поэтому чужой пустой
каталог, появившийся на исходном имени, уничтожался обеими ветками возврата — и при
несовпадении удостоверения, и после неудачного ``rmdir``. Функция возвращала ``False``,
уже уничтожив чужое: у закреплённого чужого каталога ``st_nlink`` становился нулём.

Здесь занятие имени и возврат делаются ТОЛЬКО неделимой операцией без замены
(``renameat2(RENAME_NOREPLACE)``). Занятая цель — отказ и честное сообщение о том, где
теперь лежит перемещённое, а не молчаливое уничтожение.

Проверка «а нет ли уже такого имени» с последующим ``rename`` доказательством не
считается: между проверкой и переименованием цель может появиться.

ВАЖНО о шве. Проба рецензента перехватывала ``safepath.os.rename``. После правки этот
вызов в потоке снятия не используется вовсе, поэтому её сценарий с несовпадением
удостоверения до ветки больше НЕ ДОХОДИТ. Ниже тот же сценарий воспроизведён на новом
шве ``safepath.rename_noreplace`` — чтобы проверка осталась настоящей, а не исчезла
вместе с перехватом.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gitsync import safepath
from gitsync.backends import NativeStorageBackend
from gitsync.designer import DesignerRunner, StorageAccess

IS_WIN = os.name == "nt"
pytestmark = pytest.mark.skipif(IS_WIN, reason="изоляция и возврат — путь POSIX")


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


def _pin(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


# --- примитив ------------------------------------------------------------------------

def test_rename_noreplace_refuses_occupied_destination(tmp_path):
    """Неделимое переименование не трогает занятую цель и сообщает FileExistsError."""
    (tmp_path / "src").mkdir()
    (tmp_path / "busy").mkdir()
    victim = _pin(tmp_path / "busy")
    parent = _pin(tmp_path)
    try:
        with pytest.raises(FileExistsError):
            safepath.rename_noreplace("src", "busy", parent)
        assert os.fstat(victim).st_nlink > 0, "занятая цель уничтожена"
        assert (tmp_path / "src").is_dir(), "источник исчез при отказе"
    finally:
        os.close(victim)
        os.close(parent)


def test_rename_noreplace_moves_onto_free_name(tmp_path):
    (tmp_path / "src").mkdir()
    parent = _pin(tmp_path)
    try:
        safepath.rename_noreplace("src", "fresh", parent)
    finally:
        os.close(parent)
    assert (tmp_path / "fresh").is_dir() and not (tmp_path / "src").exists()


# --- сценарий рецензента №1: несовпадение удостоверения, цель занята ------------------

def test_mismatch_restore_does_not_clobber_new_foreign_destination(tmp_path, monkeypatch):
    """Сценарий Евы mismatch_restore на НОВОМ шве: оба чужих объекта обязаны уцелеть."""
    own = tmp_path / "own"
    own.mkdir()
    fd = _pin(own)
    identity = safepath._identity_of(os.fstat(fd))
    real = safepath.rename_noreplace
    state: dict = {"calls": 0, "victim_fd": None}

    def hook(old, new, dir_fd):
        state["calls"] += 1
        if state["calls"] == 1:
            # Наш каталог уводят, на его имя кладут ПЕРВЫЙ чужой каталог…
            own.rename(tmp_path / "original-preserved")
            own.mkdir()
            result = real(old, new, dir_fd)      # …он и уезжает в изоляцию
            own.mkdir()                          # ВТОРОЙ чужой на исходном имени
            state["victim_fd"] = _pin(own)
            return result
        return real(old, new, dir_fd)

    monkeypatch.setattr(safepath, "rename_noreplace", hook)
    displaced: set[str] = set()
    try:
        returned = safepath._rmdir_emptied_posix(own, identity, fd, "S15", displaced)
        assert state["calls"] >= 1, "сценарий не сработал: шов не был задействован"
        assert returned is False, "подмена обязана давать отказ"
        assert os.fstat(state["victim_fd"]).st_nlink > 0, (
            "ЧУЖОЙ каталог на исходном имени уничтожен возвратом"
        )
        assert os.fstat(fd).st_nlink > 0, "наш исходный каталог уничтожен"
        assert displaced, "перемещённое не зарегистрировано для защиты"
        left = tmp_path / next(iter(displaced))
        assert left.is_dir(), "перемещённый чужой объект исчез"
    finally:
        os.close(fd)
        if state["victim_fd"] is not None:
            os.close(state["victim_fd"])


def test_mismatch_restore_reports_actual_location(tmp_path, monkeypatch, caplog):
    own = tmp_path / "own"
    own.mkdir()
    fd = _pin(own)
    identity = safepath._identity_of(os.fstat(fd))
    real = safepath.rename_noreplace
    state = {"calls": 0}

    def hook(old, new, dir_fd):
        state["calls"] += 1
        if state["calls"] == 1:
            own.rename(tmp_path / "original-preserved")
            own.mkdir()
            result = real(old, new, dir_fd)
            own.mkdir()
            return result
        return real(old, new, dir_fd)

    monkeypatch.setattr(safepath, "rename_noreplace", hook)
    displaced: set[str] = set()
    with caplog.at_level("WARNING"):
        safepath._rmdir_emptied_posix(own, identity, fd, "S15", displaced)
    os.close(fd)
    messages = "\n".join(caplog.messages)
    assert "занято" in messages, messages
    name = next(iter(displaced))
    assert name in messages, "в журнале нет фактического имени перемещённого"


# --- сценарий рецензента №2: неудачный rmdir, цель занята ----------------------------

def test_failed_rmdir_restore_does_not_clobber_foreign_destination(tmp_path, monkeypatch):
    """Дословный сценарий Евы failed_rmdir_restore: чужая цель обязана уцелеть."""
    own = tmp_path / "own"
    own.mkdir()
    fd = _pin(own)
    identity = safepath._identity_of(os.fstat(fd))
    real_rmdir = safepath.os.rmdir
    state: dict = {"victim_fd": None}

    def hook(name, *args, **kwargs):
        if str(name).startswith(".gitsync-discard-") and state["victim_fd"] is None:
            own.mkdir()
            state["victim_fd"] = _pin(own)
            raise OSError(39, "deterministic failed rmdir")
        return real_rmdir(name, *args, **kwargs)

    monkeypatch.setattr(safepath.os, "rmdir", hook)
    displaced: set[str] = set()
    try:
        returned = safepath._rmdir_emptied_posix(own, identity, fd, "S15", displaced)
        assert state["victim_fd"] is not None, "сценарий не сработал"
        assert returned is False
        assert os.fstat(state["victim_fd"]).st_nlink > 0, (
            "ЧУЖОЙ каталог на исходном имени уничтожен возвратом"
        )
        assert os.fstat(fd).st_nlink > 0, "наш каталог уничтожен"
        assert displaced, "перемещённое не зарегистрировано"
        assert (tmp_path / next(iter(displaced))).is_dir()
    finally:
        os.close(fd)
        if state["victim_fd"] is not None:
            os.close(state["victim_fd"])


def test_failed_rmdir_restores_when_destination_is_free(tmp_path, monkeypatch):
    """Контроль: если исходное имя свободно, свой каталог возвращается на место."""
    own = tmp_path / "own"
    own.mkdir()
    fd = _pin(own)
    identity = safepath._identity_of(os.fstat(fd))
    real_rmdir = safepath.os.rmdir
    fired = {"once": False}

    def hook(name, *args, **kwargs):
        if str(name).startswith(".gitsync-discard-") and not fired["once"]:
            fired["once"] = True
            raise OSError(39, "deterministic failed rmdir")
        return real_rmdir(name, *args, **kwargs)

    monkeypatch.setattr(safepath.os, "rmdir", hook)
    displaced: set[str] = set()
    returned = safepath._rmdir_emptied_posix(own, identity, fd, "S15", displaced)
    os.close(fd)
    assert fired["once"] and returned is False
    assert own.is_dir(), "свой каталог не вернулся на исходное имя"
    assert not displaced, "возврат удался — перемещённых быть не должно"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".gitsync-discard-")]


# --- столкновение имени изоляции -----------------------------------------------------

def test_isolation_name_collision_retries_without_clobbering(tmp_path, monkeypatch):
    """Занятое имя изоляции не замещается: берётся другое."""
    own = tmp_path / "own"
    own.mkdir()
    fd = _pin(own)
    identity = safepath._identity_of(os.fstat(fd))
    real = safepath.rename_noreplace
    state = {"calls": 0}

    def hook(old, new, dir_fd):
        state["calls"] += 1
        if state["calls"] <= 2:
            raise FileExistsError(17, "collision", new)
        return real(old, new, dir_fd)

    monkeypatch.setattr(safepath, "rename_noreplace", hook)
    returned = safepath._rmdir_emptied_posix(own, identity, fd, "S15")
    os.close(fd)
    assert state["calls"] >= 3, "столкновение не отрабатывалось повторной попыткой"
    assert returned is True, "после смены имени снятие обязано состояться"
    assert not own.exists()


def test_isolation_gives_up_when_every_name_collides(tmp_path, monkeypatch):
    own = tmp_path / "own"
    own.mkdir()
    fd = _pin(own)
    identity = safepath._identity_of(os.fstat(fd))

    def always_busy(old, new, dir_fd):
        raise FileExistsError(17, "collision", new)

    monkeypatch.setattr(safepath, "rename_noreplace", always_busy)
    returned = safepath._rmdir_emptied_posix(own, identity, fd, "S15")
    os.close(fd)
    assert returned is False
    assert own.is_dir(), "свой каталог должен остаться нетронутым"


# --- отсутствие неделимого примитива --------------------------------------------------

def test_without_atomic_primitive_cleanup_refuses(tmp_path, monkeypatch, caplog):
    """Нет renameat2 — снимать по имени нельзя: отказ, а не обычный rename."""
    own = tmp_path / "own"
    own.mkdir()
    fd = _pin(own)
    identity = safepath._identity_of(os.fstat(fd))
    monkeypatch.setattr(safepath, "_renameat2", lambda: None)
    monkeypatch.setattr(safepath, "_renameat2_state", [])
    with caplog.at_level("WARNING"):
        returned = safepath._rmdir_emptied_posix(own, identity, fd, "S15")
    os.close(fd)
    assert returned is False
    assert own.is_dir(), "без неделимого примитива каталог обязан остаться"
    assert "неделим" in "\n".join(caplog.messages).lower()


# --- учёт: перемещённое не должно быть снесено уборкой родителя ----------------------

def test_displaced_name_is_protected_from_parent_cleanup(tmp_path, monkeypatch):
    """Перемещённое под именем изоляции переживает уборку каталога запуска."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    real_rmdir = safepath.os.rmdir
    state: dict = {"victim_fd": None}

    def hook(name, *args, **kwargs):
        if str(name).startswith(".gitsync-discard-") and state["victim_fd"] is None:
            # Пока наш рабочий каталог в изоляции, на его имя кладут чужой каталог.
            (root / worker.name).mkdir()
            state["victim_fd"] = _pin(root / worker.name)
            raise OSError(39, "deterministic failed rmdir")
        return real_rmdir(name, *args, **kwargs)

    monkeypatch.setattr(safepath.os, "rmdir", hook)
    try:
        backend.cleanup()
        assert os.fstat(state["victim_fd"]).st_nlink > 0, "чужой каталог уничтожен"
        left = [p.name for p in root.iterdir() if p.name.startswith(".gitsync-discard-")]
        assert left, "перемещённый свой каталог исчез при уборке корня"
        assert root.is_dir(), "корень обязан остаться: внутри защищённое содержимое"
    finally:
        if state["victim_fd"] is not None:
            os.close(state["victim_fd"])


def test_repeat_cleanup_after_displacement_is_still_safe(tmp_path, monkeypatch):
    """Повторная уборка не уничтожает ни чужое, ни перемещённое."""
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    real_rmdir = safepath.os.rmdir
    state: dict = {"victim_fd": None}

    def hook(name, *args, **kwargs):
        if str(name).startswith(".gitsync-discard-") and state["victim_fd"] is None:
            (root / worker.name).mkdir()
            state["victim_fd"] = _pin(root / worker.name)
            raise OSError(39, "deterministic failed rmdir")
        return real_rmdir(name, *args, **kwargs)

    monkeypatch.setattr(safepath.os, "rmdir", hook)
    try:
        backend.cleanup()
        monkeypatch.setattr(safepath.os, "rmdir", real_rmdir)
        backend.cleanup()
        backend.cleanup()
        assert os.fstat(state["victim_fd"]).st_nlink > 0, "повтор уничтожил чужое"
    finally:
        if state["victim_fd"] is not None:
            os.close(state["victim_fd"])


# --- контроль: обычная работа не пострадала ------------------------------------------

def test_normal_cleanup_unchanged(tmp_path):
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    (worker / "ib").mkdir()
    (worker / "ib" / "1Cv8.1CD").write_bytes(b"x" * 16)
    backend.cleanup()
    assert not root.exists()
    assert not backend._worker_dirs
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".gitsync-discard-")]
