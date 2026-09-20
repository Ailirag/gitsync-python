"""Каталог запуска обязан быть частным для контейнера (stage-18).

ЧТО ИЗМЕРЕНО И ПОЧЕМУ ЭТО ПРОВЕРЯЕТСЯ КОДОМ. На стенде запускались два ШТАТНЫХ
контейнера с одним и тем же UID 10001 (evidence/stage-18):

* каталог запуска на bind-mount каталога хоста — нападающий видит выгрузку, читает её
  и уничтожает ``rm -rf`` (10-isolation-probe.log: ``ATTACKER_DIR_AFTER=destroyed``);
* каталог запуска на томе docker, именованном или анонимном, — то же самое
  (13-volume-probe.log: ``ATTACKER_DIR_AFTER=destroyed`` в обоих случаях);
* каталог запуска на собственном слое контейнера — нападающий не видит его вовсе
  (``ATTACKER2_SEES_DIR=false``), выгрузка цела.

Поэтому общий том под каталогом запуска не поддерживается: задание обязано ОТКАЗАТЬСЯ
до создания каталога и до любого удаления. Здесь проверяется само правило, на
синтетическом ``mountinfo``.

Часть проверок помечена ``posix_mounts_only``: они говорят о путях вида ``/work/tmp`` и о
``/proc/self/mountinfo``, а на Windows ``realpath('/work/tmp')`` даёт диск и обратные
слэши, и
сравнение мерило бы разбор путей чужой платформы, а не правило. Правило там и не
действует. Проверки, не зависящие от модели монтирований (включение требования, отказ
при отсутствии сведений, отказ ДО создания каталога), идут на обеих платформах.

Граница, которая этим НЕ закрыта и закрытой не объявляется: враждебный процесс под тем
же UID ВНУТРИ того же контейнера. Она измерена в stage-17 и остаётся открытой.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gitsync.errors import UnsafePathError
from gitsync.safepath import (
    PRIVATE_SCRATCH_ENV,
    SCRATCH_DIR_ENV,
    default_scratch_root,
    require_private_scratch,
    scratch_is_private,
)

# Строки настоящего /proc/self/mountinfo, снятые в контейнере на стенде.
OVERLAY_ROOT = "31 1 0:28 / / rw,relatime - overlay overlay rw,lowerdir=/x,upperdir=/y"
BIND_WORK = ("42 31 8:48 /srv/gitsync-native14/fix19/live/shared /work rw,relatime "
             "- ext4 /dev/sdd rw")
VOLUME_WORKTMP = ("43 31 8:48 /var/lib/docker/volumes/worktmp/_data /work/tmp rw,relatime "
                  "- ext4 /dev/sdd rw")
TMPFS_SCRATCH = "44 31 0:99 / /scratch rw,nosuid,nodev - tmpfs tmpfs rw,mode=700"
PROC = "45 31 0:24 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw"

#: Модель монтирований здесь — POSIX: точки вида ``/work/tmp`` и ``/proc/self/mountinfo``.
#: На Windows ``realpath('/work/tmp')`` даёт ``C:\\work\\tmp``, и синтетический
#: ``mountinfo`` с ним не совпадает — проверка мерила бы не правило, а разбор путей чужой
#: платформы. Само правило там и не действует: ``mountinfo`` нет, требование поставки
#: (:data:`PRIVATE_SCRATCH_ENV`) выставляет образ, а на Windows его никто не ставит.
#: Это НЕ «пропустим неудобное»: поведение вне контейнера проверяется отдельно ниже и на
#: обеих платформах — см. ``test_outside_the_container_delivery_nothing_changes`` и
#: ``test_without_mount_information_the_demand_fails_closed``.
posix_mounts_only = pytest.mark.skipif(
    os.name == "nt",
    reason="модель монтирований POSIX: правило читает /proc/self/mountinfo",
)


@pytest.fixture()
def mountinfo(tmp_path, monkeypatch):
    """Подменяет источник сведений о монтированиях на подготовленный файл."""
    def install(*lines: str) -> Path:
        path = tmp_path / "mountinfo"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setattr("gitsync.safepath._MOUNTINFO", str(path))
        return path
    return install


@pytest.fixture()
def demanded(monkeypatch):
    """Контейнерная поставка: требование частного каталога включено."""
    monkeypatch.setenv(PRIVATE_SCRATCH_ENV, "1")


# --- само правило -------------------------------------------------------------


@posix_mounts_only
def test_the_container_own_layer_is_private(mountinfo):
    mountinfo(OVERLAY_ROOT, PROC)
    private, backing = scratch_is_private("/var/lib/gitsync/scratch")
    assert private, backing
    assert backing["fstype"] == "overlay"


@posix_mounts_only
def test_a_tmpfs_raised_inside_the_container_is_private(mountinfo):
    mountinfo(OVERLAY_ROOT, TMPFS_SCRATCH, PROC)
    private, backing = scratch_is_private("/scratch/run-1")
    assert private, backing
    assert backing["fstype"] == "tmpfs"


@posix_mounts_only
def test_a_host_bind_mount_is_not_private(mountinfo):
    """Именно этот случай замерен как уничтожаемый соседним контейнером."""
    mountinfo(OVERLAY_ROOT, BIND_WORK, PROC)
    private, backing = scratch_is_private("/work/.gitsync-tmp")
    assert not private
    assert backing["mount_point"] == "/work"


@posix_mounts_only
def test_a_docker_volume_is_not_private(mountinfo):
    """Том docker замерен так же, как bind-mount: сосед читает и уничтожает."""
    mountinfo(OVERLAY_ROOT, VOLUME_WORKTMP, PROC)
    private, backing = scratch_is_private("/work/tmp")
    assert not private
    assert backing["mount_point"] == "/work/tmp"


@posix_mounts_only
def test_the_nearest_mount_wins_not_the_first_one(mountinfo):
    """Вложенные точки монтирования: отвечать обязана ближайшая, иначе ответ случаен."""
    mountinfo(OVERLAY_ROOT, BIND_WORK, VOLUME_WORKTMP, PROC)
    private, backing = scratch_is_private("/work/tmp/run-1")
    assert not private
    assert backing["mount_point"] == "/work/tmp", "выбрана не ближайшая точка"


# --- отказ ---------------------------------------------------------------------


@posix_mounts_only
def test_a_shared_scratch_is_refused_and_names_the_mount(mountinfo, demanded):
    mountinfo(OVERLAY_ROOT, VOLUME_WORKTMP, PROC)
    with pytest.raises(UnsafePathError) as refusal:
        require_private_scratch("/work/tmp")
    message = str(refusal.value)
    assert "/work/tmp" in message, message
    assert "Ничего не создано и не удалено" in message, message


@posix_mounts_only
def test_a_private_scratch_is_accepted(mountinfo, demanded):
    mountinfo(OVERLAY_ROOT, PROC)
    require_private_scratch("/var/lib/gitsync/scratch")   # не поднимает исключения


def test_without_mount_information_the_demand_fails_closed(mountinfo, demanded, tmp_path,
                                                           monkeypatch):
    """Нет сведений — отказ, а не «наверное, всё хорошо»."""
    monkeypatch.setattr("gitsync.safepath._MOUNTINFO", str(tmp_path / "нет-такого-файла"))
    with pytest.raises(UnsafePathError):
        require_private_scratch("/var/lib/gitsync/scratch")


def test_outside_the_container_delivery_nothing_changes(mountinfo, monkeypatch):
    """Без требования поставки правило молчит: Windows и обычная установка не трогаются."""
    monkeypatch.delenv(PRIVATE_SCRATCH_ENV, raising=False)
    mountinfo(OVERLAY_ROOT, VOLUME_WORKTMP, PROC)
    require_private_scratch("/work/tmp")   # общий том, и это осознанный режим оператора


@pytest.mark.parametrize("value", ["0", "", "no", "false", "  "])
def test_only_an_explicit_demand_turns_the_rule_on(mountinfo, monkeypatch, value):
    monkeypatch.setenv(PRIVATE_SCRATCH_ENV, value)
    mountinfo(OVERLAY_ROOT, VOLUME_WORKTMP, PROC)
    require_private_scratch("/work/tmp")


@pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
@posix_mounts_only
def test_the_demand_is_recognised_in_the_usual_spellings(mountinfo, monkeypatch, value):
    monkeypatch.setenv(PRIVATE_SCRATCH_ENV, value)
    mountinfo(OVERLAY_ROOT, VOLUME_WORKTMP, PROC)
    with pytest.raises(UnsafePathError):
        require_private_scratch("/work/tmp")


# --- умолчание каталога запуска ------------------------------------------------


def test_the_delivery_declares_the_default_scratch(monkeypatch):
    monkeypatch.setenv(SCRATCH_DIR_ENV, "/var/lib/gitsync/scratch")
    assert default_scratch_root(Path("/work/.gitsync-tmp")) == Path("/var/lib/gitsync/scratch")


def test_without_the_declaration_the_old_default_stays(monkeypatch):
    """Вне контейнера умолчание прежнее — рядом с рабочей копией."""
    monkeypatch.delenv(SCRATCH_DIR_ENV, raising=False)
    assert default_scratch_root(Path("/work/.gitsync-tmp")) == Path("/work/.gitsync-tmp")


def test_an_empty_declaration_is_not_a_path(monkeypatch):
    monkeypatch.setenv(SCRATCH_DIR_ENV, "   ")
    assert default_scratch_root(Path("/work/.gitsync-tmp")) == Path("/work/.gitsync-tmp")


# --- правило применяется там, где создаётся каталог ----------------------------


def test_the_sync_manager_refuses_before_creating_anything(mountinfo, demanded, tmp_path,
                                                           monkeypatch):
    """Отказ обязан случиться ДО mkdir: иначе «до удаления» не значит ничего."""
    from gitsync.sync import SyncManager, SyncOptions

    repo = tmp_path / "repo"
    repo.mkdir()
    shared = tmp_path / "shared"
    mountinfo(OVERLAY_ROOT,
              f"50 31 8:48 /host/shared {shared} rw,relatime - ext4 /dev/sdd rw",
              PROC)
    manager = SyncManager(str(repo), None, SyncOptions(temp_root=str(shared)))
    with pytest.raises(UnsafePathError):
        manager._temp_root()
    assert not shared.exists(), "каталог создан до отказа: проверка стоит не там"


def test_the_native_backend_refuses_before_creating_anything(mountinfo, demanded, tmp_path):
    from gitsync.backends import NativeStorageBackend
    from gitsync.designer import DesignerRunner, StorageAccess

    shared = tmp_path / "shared" / "native-1"
    mountinfo(OVERLAY_ROOT,
              f"51 31 8:48 /host/shared {shared.parent} rw,relatime - ext4 /dev/sdd rw",
              PROC)
    backend = NativeStorageBackend(
        access=StorageAccess(path="tcp://x/y", user="u", password=None),
        runner=DesignerRunner(v8_path="/opt/1cv8/x86_64/8.3.27.2130/1cv8",
                              out_dir=shared / "designer-out", version="8.3.27.2130"),
        temp_root=shared, owns_temp_root=True,
    )
    with pytest.raises(UnsafePathError):
        backend._ensure_scratch_root()
    assert not shared.exists(), "каталог создан до отказа: проверка стоит не там"


def test_the_image_declares_the_private_scratch():
    """Договор обязан быть в образе, иначе он не действует нигде."""
    dockerfile = (Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.core")
    text = dockerfile.read_text(encoding="utf-8")
    assert "GITSYNC_SCRATCH=/var/lib/gitsync/scratch" in text
    assert "GITSYNC_REQUIRE_PRIVATE_SCRATCH=1" in text
    assert "/var/lib/gitsync/scratch" in text.split("ENV HOME")[0], \
        "каталог обязан создаваться в образе, а не возникать точкой монтирования"


@posix_mounts_only
def test_a_scratch_that_does_not_exist_yet_is_still_judged_correctly(mountinfo, demanded):
    """Каталога ещё нет — и это норма: отказ обязан быть ДО создания.

    Если бы точка монтирования определялась по существующему предку, несозданный
    каталог на общем томе выглядел бы как частный, и проверка пропускала бы ровно тот
    случай, ради которого написана.
    """
    mountinfo(OVERLAY_ROOT, VOLUME_WORKTMP, PROC)
    assert not Path("/work/tmp/run-которого-нет").exists()
    private, backing = scratch_is_private("/work/tmp/run-которого-нет")
    assert not private
    assert backing["mount_point"] == "/work/tmp"
