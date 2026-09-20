"""Stage-16: снятие по имени после изоляции не имеет права давать ложный успех (P1 EVA-14).

ВОСПРОИЗВЕДЕНИЕ ЗАМЕЧАНИЯ приёмки. После неделимой изоляции (stage-15) финальное снятие
СВОЕГО каталога всё равно идёт ПО ИМЕНИ: ``os.rmdir(isolated, dir_fd=parent)``. Между
сверкой ``os.stat(isolated, ...)`` и этим ``rmdir`` тот же пользователь успевает увести
изолированный СВОЙ каталог и подставить на имя изоляции ЧУЖОЙ пустой. Тогда ``rmdir``
снимает ЧУЖОЙ каталог, а функция возвращала ``True`` — при живом своём каталоге
(``st_nlink`` закреплённого дескриптора больше нуля). Получалось удаление чужого плюс
ложный успех.

Полностью закрыть уничтожение чужого ПУСТОГО каталога в этом окне нельзя: удаления
каталога по дескриптору в POSIX не существует. Но ложный успех закрыть обязаны: после
``rmdir`` проверяется, что ИСЧЕЗ ИМЕННО НАШ объект (``st_nlink == 0``); иначе — громкая
ошибка и отказ, никогда ``True``.

Шов здесь — ``safepath.os.rmdir``, тот же, что у пробы рецензента
``evidence/eva-review-23/eva-review-23-probe.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gitsync import safepath
from gitsync.backends import NativeStorageBackend
from gitsync.designer import DesignerRunner, StorageAccess

IS_WIN = os.name == "nt"
pytestmark = pytest.mark.skipif(IS_WIN, reason="снятие по имени после изоляции — путь POSIX")


@pytest.fixture(autouse=True)
def _session_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GITSYNC_SESSION_DIR", str(tmp_path / "sessions"))


def _pin(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _backend(root: Path, *, owns: bool = True) -> NativeStorageBackend:
    return NativeStorageBackend(
        access=StorageAccess(path=str(root.parent / "storage"), user="probe"),
        runner=DesignerRunner("1cv8", root / "designer-out"),
        temp_root=root,
        owns_temp_root=owns,
        ib_factory=lambda worker_dir: "unused",
    )


def _swap_isolated_for_foreign(tmp_path: Path, state: dict):
    """Шов рецензента: уводит изолированный СВОЙ каталог и кладёт на его имя ЧУЖОЙ."""
    real_rmdir = safepath.os.rmdir

    def hooked(name, *args, **kwargs):
        if str(name).startswith(".gitsync-discard-") and not state["hits"]:
            state["hits"].append(str(name))
            dir_fd = kwargs["dir_fd"]
            os.rename(name, "saved-own", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.rename("foreign", name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return real_rmdir(name, *args, **kwargs)

    return hooked


# --- дословный сценарий рецензента ---------------------------------------------------

def test_foreign_swapped_onto_isolation_name_is_not_a_success(tmp_path, monkeypatch):
    """Подмена на имя изоляции обязана давать ОТКАЗ, а не ложный успех."""
    own = tmp_path / "own"
    own.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    fd = _pin(own)
    foreign_fd = _pin(foreign)
    identity = safepath._identity_of(os.fstat(fd))
    state: dict = {"hits": []}
    monkeypatch.setattr(safepath.os, "rmdir", _swap_isolated_for_foreign(tmp_path, state))
    try:
        returned = safepath._rmdir_emptied_posix(own, identity, fd, "S16")
        assert len(state["hits"]) >= 1, "шов не был задействован: проба ничего не проверила"
        assert returned is False, (
            "ложный успех: снят чужой объект, а очистка объявлена состоявшейся"
        )
        assert os.fstat(fd).st_nlink > 0, "наш каталог жив — это и есть признак подмены"
    finally:
        os.close(fd)
        os.close(foreign_fd)


def test_swap_is_reported_as_error_with_actual_state(tmp_path, monkeypatch, caplog):
    """Отказ обязан быть громким и называть фактическое состояние."""
    own = tmp_path / "own"
    own.mkdir()
    (tmp_path / "foreign").mkdir()
    fd = _pin(own)
    identity = safepath._identity_of(os.fstat(fd))
    state: dict = {"hits": []}
    monkeypatch.setattr(safepath.os, "rmdir", _swap_isolated_for_foreign(tmp_path, state))
    with caplog.at_level("ERROR"):
        returned = safepath._rmdir_emptied_posix(own, identity, fd, "S16")
    os.close(fd)
    assert returned is False
    messages = "\n".join(caplog.messages)
    assert "nlink" in messages.lower() or "ссыл" in messages.lower(), messages
    assert state["hits"][0] in messages, "в журнале нет имени изоляции, на котором произошла подмена"


def test_swapped_own_directory_survives_and_is_not_reported_cleaned(tmp_path, monkeypatch):
    """Уведённый свой каталог остаётся жив, и это не объявляется уборкой."""
    own = tmp_path / "own"
    own.mkdir()
    (own / "marker.txt").write_text("наш файл", encoding="utf-8")
    (tmp_path / "foreign").mkdir()
    fd = _pin(own)
    identity = safepath._identity_of(os.fstat(fd))
    # Содержимое снимается ДО финального rmdir, поэтому маркер тут не переживёт;
    # проверяется именно живой каталог, а не его содержимое.
    state: dict = {"hits": []}
    monkeypatch.setattr(safepath.os, "rmdir", _swap_isolated_for_foreign(tmp_path, state))
    returned = safepath._rmdir_emptied_posix(own, identity, fd, "S16")
    alive = os.fstat(fd).st_nlink
    os.close(fd)
    assert returned is False
    assert alive > 0, "наш каталог исчез, хотя снимали подставленный чужой"
    assert (tmp_path / "saved-own").is_dir(), "уведённый свой каталог не найден"


# --- контроль: обычное снятие по-прежнему успешно ------------------------------------

def test_normal_final_rmdir_still_reports_success(tmp_path):
    """Без подмены снятие обязано давать True и обнулять ссылки нашего каталога."""
    own = tmp_path / "own"
    own.mkdir()
    fd = _pin(own)
    identity = safepath._identity_of(os.fstat(fd))
    returned = safepath._rmdir_emptied_posix(own, identity, fd, "S16")
    nlink = os.fstat(fd).st_nlink
    os.close(fd)
    assert returned is True, "обычное снятие перестало считаться успехом"
    assert nlink == 0, "каталог не снят, но успех объявлен"
    assert not own.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".gitsync-discard-")]


def test_normal_backend_cleanup_unchanged(tmp_path):
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    (worker / "ib").mkdir()
    (worker / "ib" / "1Cv8.1CD").write_bytes(b"x" * 16)
    backend.cleanup()
    assert not root.exists()
    assert not backend._worker_dirs


def test_swap_at_worker_seam_is_loud_and_spares_outside_neighbours(tmp_path, monkeypatch, caplog):
    """Подмена на шве РАБОЧЕГО каталога: отказ виден в журнале, соседи снаружи целы.

    Измеренная граница, названная прямо: сам каталог запуска после этого снимается —
    всё, что осталось внутри него, принадлежит прогону (уведённый рабочий каталог лежит
    там же под чужим для нас именем). Чужого при этом не уничтожается ничего, кроме
    подставленного в окно пустого каталога — это задокументированный порог POSIX.
    """
    root = tmp_path / "scratch"
    backend = _backend(root)
    worker, _ = backend._worker_context()
    (root / "foreign").mkdir()
    bystander = tmp_path / "foreign-bystander"
    bystander.mkdir()
    bystander_fd = _pin(bystander)
    inner = os.stat(root)
    real_rmdir = safepath.os.rmdir
    state: dict = {"hits": []}

    def hooked(name, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        here = os.fstat(dir_fd) if dir_fd is not None else None
        if here is not None and (here.st_dev, here.st_ino) == (inner.st_dev, inner.st_ino) \
                and str(name).startswith(".gitsync-discard-") and not state["hits"]:
            state["hits"].append(str(name))
            os.rename(name, "saved-own", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.rename("foreign", name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return real_rmdir(name, *args, **kwargs)

    monkeypatch.setattr(safepath.os, "rmdir", hooked)
    with caplog.at_level("ERROR"):
        backend.cleanup()
    try:
        assert state["hits"], "шов рабочего каталога не сработал"
        errors = [m for m in caplog.messages if state["hits"][0] in m]
        assert errors, "подмена на шве рабочего каталога прошла без громкого отказа"
        assert os.fstat(bystander_fd).st_nlink > 0, "уборка вышла за свой каталог запуска"
        assert bystander.is_dir()
    finally:
        os.close(bystander_fd)


def test_swap_at_scratch_root_is_refused_and_repeat_stays_safe(tmp_path, monkeypatch):
    """Подмена на шве самого каталога запуска: отказ, и повтор ничего чужого не трогает.

    Шов наводится ИМЕННО на снятие каталога запуска (родитель — ``tmp_path``), а не на
    снятие рабочего каталога внутри него: у этих двух швов разные последствия, и смешивать
    их в одной пробе нельзя.
    """
    root = tmp_path / "scratch"
    backend = _backend(root)
    backend._worker_context()
    (tmp_path / "foreign").mkdir()
    bystander = tmp_path / "foreign-bystander"
    bystander.mkdir()
    bystander_fd = _pin(bystander)
    outer = os.stat(tmp_path)
    real_rmdir = safepath.os.rmdir
    state: dict = {"hits": []}

    def hooked(name, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        at_root = dir_fd is not None and \
            (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino) == (outer.st_dev, outer.st_ino)
        if at_root and str(name).startswith(".gitsync-discard-") and not state["hits"]:
            state["hits"].append(str(name))
            os.rename(name, "saved-own", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.rename("foreign", name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return real_rmdir(name, *args, **kwargs)

    monkeypatch.setattr(safepath.os, "rmdir", hooked)
    try:
        backend.cleanup()
        assert state["hits"], "шов каталога запуска не сработал"
        assert backend._scratch_created, (
            "очистка объявлена состоявшейся, хотя снят был подставленный объект"
        )
        assert (tmp_path / "saved-own").is_dir(), "уведённый каталог запуска не найден"
        # Повтор: исходного имени уже нет, уборке нечего искать — и она обязана молча
        # ничего не трогать, а не пойти сносить соседей.
        monkeypatch.setattr(safepath.os, "rmdir", real_rmdir)
        backend.cleanup()
        backend.cleanup()
        assert os.fstat(bystander_fd).st_nlink > 0, "повтор уничтожил соседний чужой каталог"
        assert bystander.is_dir()
        assert (tmp_path / "saved-own").is_dir(), "повтор снёс уведённый каталог запуска"
    finally:
        os.close(bystander_fd)
