"""Fix17: владение и конец жизни одноразового каталога запуска нативного бэкенда.

ВОСПРОИЗВЕДЕНИЕ ДЕФЕКТА P2-1 приёмки16. После обычного `sync-all` с `init: true`
в корне временных файлов оставались полная временная файловая ИБ
(`native-*/worker-*/ib/1Cv8.1CD` и журналы) и протоколы `/Out` УСПЕШНЫХ запусков
(`native-*/designer-out/*.log`) — около 3.3 МБ за прогон, без верхней границы при
работе по расписанию. Причина: `cleanup()` снимал только каталоги `worker-*`, сам
каталог запуска `native-<uuid>` не удалял никто, а `init` вообще не вызывал `cleanup()`.

Здесь проверяется ОДНО поведение: у каталога запуска есть явный владелец и конец
жизни. Границы поведения разведены намеренно:

* одноразовое состояние (временные ИБ, `.cf`, отчёт) удаляется всегда;
* диагностика отказа переживает очистку: на путь файла `/Out` ссылается текст
  ошибки (docs/setup-guide.md, docs/docker-guide.md — «откройте протокол
  конфигуратора»), и удалить его значило бы оборвать документированный разбор;
* каталог, созданный НЕ этим запуском (внешний или постоянный), не удаляется никогда;
* `--keep-temp` сохраняет всё, включая успешные прогоны.

Запуск процесса подменяется, всё остальное настоящее: те же argv, те же каталоги,
те же артефакты и тот же путь CLI → `_build_backend` → `NativeStorageBackend`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitsync.backends import NativeStorageBackend
from gitsync.cli import main
from gitsync.designer import DesignerRunner, StorageAccess, out_file_from_args
from gitsync.errors import CancelledError, DesignerError, GitSyncError
from gitsync.locks import exclusive_lock
from gitsync.sync import resolve_lock_path
from support.native_report import ReportVersion, build_report_mxl

REPORT = build_report_mxl(
    [
        ReportVersion(1, "Иванов", "15.09.2026", "10:20:30", "Первая версия"),
        ReportVersion(2, "Петров", "16.09.2026", "08:00:00", "Вторая версия"),
    ]
)

#: Размер подставной ИБ: дефект измерялся в мегабайтах, и «пусто/не пусто» его не описывает.
IB_BYTES = 64 * 1024


@pytest.fixture()
def fake_designer(monkeypatch, tmp_path):
    """Подменяет ТОЛЬКО запуск процесса конфигуратора.

    Артефакты создаются там же, где их создаёт платформа: файловая ИБ в каталоге
    рабочего потока, отчёт и `.cf` рядом с ней, протокол — в файле из ключа `/Out`.
    Реальная платформа пишет `/Out` и при успехе, поэтому пишет и подмена: именно эти
    файлы и накапливались.
    """
    # Блокировки сессий хранилища — в tmp_path, а не в профиле запускающего.
    monkeypatch.setenv("GITSYNC_SESSION_DIR", str(tmp_path / "sessions"))
    state = SimpleNamespace(calls=[], out_files=[], fail_on=None, cancel_on=None, cancel=None)

    def run(self, args, timeout=None):
        args = list(args)
        state.calls.append(args)
        out = out_file_from_args(args)
        if out:
            state.out_files.append(Path(out))
            Path(out).write_text("﻿Протокол конфигуратора\n", encoding="utf-8")
        if state.fail_on is not None and state.fail_on in args:
            raise DesignerError(
                f"Конфигуратор завершился с кодом 1: {args[0]} ...\n"
                f"Сообщение конфигуратора ({out}):\n"
                "Не найдена лицензия. Не обнаружен ключ защиты программы"
            )
        if state.cancel_on is not None and state.cancel_on in args and state.cancel is not None:
            state.cancel.set()
        if "CREATEINFOBASE" in args:
            spec = next(item for item in args if item.startswith("File="))
            base = Path(spec[len("File="):])
            base.mkdir(parents=True, exist_ok=True)
            (base / "1Cv8.1CD").write_bytes(b"\x00" * IB_BYTES)
            (base / "1Cv8Log").mkdir(exist_ok=True)
            (base / "1Cv8Log" / "1Cv8.lgf").write_bytes(b"log")
        for key, payload in (("/ConfigurationRepositoryReport", REPORT),
                             ("/ConfigurationRepositoryDumpCfg", b"CF" * 512)):
            if key in args:
                Path(args[args.index(key) + 1]).write_bytes(payload)
        if "/DumpConfigToFiles" in args:
            dest = Path(args[args.index("/DumpConfigToFiles") + 1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(DesignerRunner, "run", run)
    return state


def _native_args(work: Path, temp_root: Path, storage: Path) -> list[str]:
    return [
        "--workdir", str(work),
        "--backend", "native",
        "--v8-path", "1cv8",
        "--storage-path", str(storage),
        "--storage-user", "reader",
        "--temp-root", str(temp_root),
        "--jobs", "1",
    ]


@pytest.fixture()
def temp_root(tmp_path):
    """Общий корень временных файлов с ЧУЖИМ содержимым, как на постоянном томе."""
    root = tmp_path / "tmp"
    (root / "чужой-каталог").mkdir(parents=True)
    (root / "чужой-каталог" / "sentinel.txt").write_bytes(b"FOREIGN")
    (root / "sentinel-root.txt").write_bytes(b"FOREIGN ROOT")
    return root


def _foreign_intact(root: Path) -> bool:
    return ((root / "чужой-каталог" / "sentinel.txt").read_bytes() == b"FOREIGN"
            and (root / "sentinel-root.txt").read_bytes() == b"FOREIGN ROOT")


def _own_leftovers(root: Path) -> list[str]:
    """Собственные каталоги инструмента, оставшиеся в общем корне."""
    return sorted(item.name for item in root.iterdir()
                  if item.name.startswith(("native-", "run-")))


def _leftover_bytes(root: Path) -> int:
    return sum(path.stat().st_size
               for name in _own_leftovers(root)
               for path in (root / name).rglob("*") if path.is_file())


def test_normal_native_sync_removes_its_own_scratch(tmp_path, temp_root, fake_designer):
    """Обычный успешный прогон не оставляет ни временной ИБ, ни протоколов."""
    work = tmp_path / "рабочая копия"

    code = main(["sync", *_native_args(work, temp_root, tmp_path / "storage")])

    assert code == 0
    assert (work / "VERSION").read_text(encoding="utf-8").count("<VERSION>2</VERSION>") == 1
    assert _own_leftovers(temp_root) == [], (
        f"после успешного прогона осталось {_leftover_bytes(temp_root)} байт собственных файлов"
    )
    assert _foreign_intact(temp_root), "очистка тронула чужие файлы в общем корне"


def test_init_alone_removes_its_own_scratch(tmp_path, temp_root, fake_designer):
    """`init` создаёт бэкенд и полноценную временную ИБ — и обязан их отпускать."""
    work = tmp_path / "новая копия"

    code = main(["init", *_native_args(work, temp_root, tmp_path / "storage")])

    assert code == 0
    assert "Иванов=Иванов <Иванов@localhost>" in (work / "AUTHORS").read_text(encoding="utf-8")
    assert _own_leftovers(temp_root) == [], (
        f"init оставил {_leftover_bytes(temp_root)} байт: именно так и накапливалась временная ИБ"
    )
    assert _foreign_intact(temp_root)


def test_sync_all_with_init_leaves_nothing_behind(tmp_path, temp_root, fake_designer):
    """Точный сценарий приёмки16: sync-all с init:true, два бэкенда за один прогон."""
    config = {
        "defaults": {
            "backend": "native", "v8_path": "1cv8", "storage_user": "reader",
            "temp_root": str(temp_root), "jobs": 1, "init": True,
        },
        "storages": [{"name": "Конфигурация", "workdir": str(tmp_path / "wc"),
                      "storage_path": str(tmp_path / "storage")}],
    }
    config_path = tmp_path / "хранилища.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    code = main(["sync-all", "--config", str(config_path)])

    assert code == 0
    assert "<VERSION>2</VERSION>" in (tmp_path / "wc" / "VERSION").read_text(encoding="utf-8")
    assert _own_leftovers(temp_root) == [], (
        f"sync-all с init оставил {_leftover_bytes(temp_root)} байт "
        "(дефект P2-1: полная временная ИБ плюс designer-out двух бэкендов)"
    )
    assert _foreign_intact(temp_root)


def test_repeated_runs_do_not_accumulate(tmp_path, temp_root, fake_designer):
    """Три запуска подряд: постоянный том не должен расти от запуска к запуску."""
    work = tmp_path / "рабочая копия"
    for _ in range(3):
        assert main(["sync", *_native_args(work, temp_root, tmp_path / "storage")]) == 0
    assert _own_leftovers(temp_root) == []
    assert _foreign_intact(temp_root)


@pytest.mark.parametrize("command", ["sync", "init"])
def test_failure_before_the_first_designer_run_leaves_no_scratch(
    tmp_path, temp_root, fake_designer, monkeypatch, command
):
    """Отказ подготовки ДО первого обращения к конфигуратору не создаёт каталог запуска.

    Подготовка репозитория идёт после построения бэкенда. Если каталог запуска
    создаётся заранее, каждый такой отказ оставляет пустой `native-<uuid>`: на
    постоянном томе регулярного расписания это тот же неограниченный рост, только
    инодами вместо мегабайт. Каталог обязан появляться от первой настоящей работы.
    """
    from gitsync.gitrepo import GitRepo

    def refuse(self, *args, **kwargs):
        raise GitSyncError("git отказал в подготовке рабочей копии")

    monkeypatch.setattr(GitRepo, "init", refuse)
    work = tmp_path / "рабочая копия"

    code = main([command, *_native_args(work, temp_root, tmp_path / "storage")])

    assert code != 0, "отказ подготовки обязан быть отказом команды"
    assert not fake_designer.calls, "конфигуратор не должен был запускаться"
    assert _own_leftovers(temp_root) == [], "пустой каталог запуска — та же утечка"
    assert _foreign_intact(temp_root)


@pytest.mark.parametrize(
    ("stage", "marker"),
    [("before_bootstrap", "CREATEINFOBASE"),
     ("after_bootstrap", "/ConfigurationRepositoryReport"),
     ("during_export", "/ConfigurationRepositoryDumpCfg")],
)
def test_failure_keeps_diagnostics_but_not_the_temporary_infobase(
    tmp_path, temp_root, fake_designer, stage, marker
):
    """Отказ: протоколы `/Out` целы (на них ссылается ошибка), временная ИБ снята."""
    fake_designer.fail_on = marker
    work = tmp_path / "рабочая копия"

    code = main(["sync", *_native_args(work, temp_root, tmp_path / "storage")])

    assert code != 0, "отказ конфигуратора обязан быть отказом прогона"
    assert fake_designer.out_files, "конфигуратор обязан получать /Out на каждом запуске"
    for path in fake_designer.out_files:
        assert path.is_file(), (
            f"удалён протокол, на путь которого ссылается текст ошибки: {path}"
        )
    leftovers = _own_leftovers(temp_root)
    assert leftovers, "каталог с диагностикой отказа обязан остаться"
    for name in leftovers:
        assert not list((temp_root / name).glob("worker-*")), "временная ИБ не является диагностикой"
        assert not list((temp_root / name).rglob("1Cv8.1CD")), "файловая ИБ обязана быть удалена"
    assert _foreign_intact(temp_root)


def test_keep_temp_preserves_infobase_and_logs(tmp_path, temp_root, fake_designer):
    """`--keep-temp` — разбор инцидента: сохраняется всё, включая успешный прогон."""
    work = tmp_path / "рабочая копия"

    code = main(["sync", *_native_args(work, temp_root, tmp_path / "storage"), "--keep-temp"])

    assert code == 0
    kept = _own_leftovers(temp_root)
    assert any(name.startswith("native-") for name in kept)
    assert [path for name in kept for path in (temp_root / name).rglob("1Cv8.1CD")], (
        "--keep-temp обязан сохранять временную ИБ для разбора инцидента"
    )
    assert all(path.is_file() for path in fake_designer.out_files)
    assert _foreign_intact(temp_root)


def test_busy_target_lock_leaves_no_scratch(tmp_path, temp_root, fake_designer):
    """Цель занята другим процессом: свой каталог запуска всё равно не остаётся."""
    work = tmp_path / "рабочая копия"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True)
    with exclusive_lock(resolve_lock_path(work), timeout=0):
        code = main(["init", *_native_args(work, temp_root, tmp_path / "storage"),
                     "--lock-timeout", "0"])

    assert code != 0
    assert _own_leftovers(temp_root) == [], "пустой каталог запуска тоже накапливается"


# --- границы владения ------------------------------------------------------


def _owned_backend(root: Path, **kwargs) -> NativeStorageBackend:
    return NativeStorageBackend(
        access=StorageAccess(path=str(root.parent / "storage"), user="reader"),
        runner=DesignerRunner("1cv8", root / "designer-out"),
        temp_root=root,
        owns_temp_root=True,
        **kwargs,
    )


def _used_backend(root: Path, **kwargs) -> NativeStorageBackend:
    """Бэкенд, уже сделавший настоящую работу: каталог запуска создан первым обращением.

    Требует фикстуру `fake_designer`: каталог появляется от вызова конфигуратора, а не
    от построения объекта, поэтому границы владения проверяются на реальном состоянии
    (временная ИБ, отчёт, протоколы), а не на пустом каталоге.
    """
    backend = _owned_backend(root, **kwargs)
    assert not root.exists(), "каталог запуска не должен появляться до первой работы"
    backend.fetch_history(1)
    assert (root / "designer-out").is_dir() and list(root.glob("worker-*")), (
        "первое обращение обязано было создать каталог запуска с временной ИБ"
    )
    return backend


def test_external_temp_root_is_never_removed(tmp_path, monkeypatch):
    """Каталог, созданный не этим запуском, снимать нельзя: он может быть постоянным."""
    monkeypatch.setenv("GITSYNC_SESSION_DIR", str(tmp_path / "sessions"))
    external = tmp_path / "постоянные данные"
    external.mkdir()
    (external / "cache.bin").write_bytes(b"PERSISTENT")
    backend = NativeStorageBackend(
        access=StorageAccess(path=str(tmp_path / "storage"), user="reader"),
        runner=DesignerRunner("1cv8", external / "designer-out"),
        temp_root=external,
    )

    backend.cleanup()

    assert external.is_dir(), "удалён внешний каталог, который инструмент не создавал"
    assert (external / "cache.bin").read_bytes() == b"PERSISTENT"


def test_cleanup_is_idempotent(tmp_path, fake_designer):
    """Повторная очистка не обязана находить свои каталоги и не имеет права падать."""
    root = tmp_path / "tmp" / "native-1"
    backend = _used_backend(root)

    backend.cleanup()
    backend.cleanup()

    assert not root.exists()
    assert (tmp_path / "tmp").is_dir(), "общий родитель не принадлежит запуску"


def test_unused_backend_creates_nothing(tmp_path, monkeypatch):
    """Бэкенд, которым не воспользовались, не оставляет даже пустого каталога."""
    monkeypatch.setenv("GITSYNC_SESSION_DIR", str(tmp_path / "sessions"))
    root = tmp_path / "tmp" / "native-unused"
    backend = _owned_backend(root)

    backend.cleanup()

    assert not root.exists()
    assert not (tmp_path / "tmp").exists(), "общий родитель тоже не должен появляться"


def test_cancellation_does_not_retain_scratch(tmp_path, fake_designer):
    """Отмена — не отказ: повторные остановки не должны копить каталоги."""
    import threading

    root = tmp_path / "tmp" / "native-cancel"
    backend = _used_backend(root)
    cancel = threading.Event()
    fake_designer.cancel = cancel
    fake_designer.cancel_on = "/ConfigurationRepositoryDumpCfg"

    with pytest.raises(CancelledError):
        backend.export_version(1, tmp_path / "xml", cancel)
    backend.cleanup()

    assert not root.exists(), "отменённый прогон оставил каталог запуска"


@pytest.mark.skipif(os.name != "nt", reason="настоящий junction NTFS")
def test_junction_inside_scratch_does_not_reach_outside(tmp_path, fake_designer):
    """Соединение внутри каталога запуска снимается как ссылка, цель остаётся целой."""
    external = tmp_path / "external"
    external.mkdir()
    (external / "precious.txt").write_bytes(b"MUST SURVIVE")
    root = tmp_path / "tmp" / "native-junction"
    backend = _used_backend(root)
    link = root / "junction"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(external)],
                   check=True, capture_output=True)
    assert link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT

    backend.cleanup()

    assert (external / "precious.txt").read_bytes() == b"MUST SURVIVE"
    if link.exists():  # очистка отказалась — тоже безопасный исход
        os.rmdir(link)


@pytest.mark.skipif(os.name == "nt", reason="символьная ссылка POSIX")
def test_symlink_inside_scratch_does_not_reach_outside(tmp_path, fake_designer):
    """То же для POSIX: удаляется сама ссылка, а не дерево за ней."""
    external = tmp_path / "external"
    external.mkdir()
    (external / "precious.txt").write_bytes(b"MUST SURVIVE")
    root = tmp_path / "tmp" / "native-symlink"
    backend = _used_backend(root)
    (root / "link").symlink_to(external, target_is_directory=True)

    backend.cleanup()

    assert (external / "precious.txt").read_bytes() == b"MUST SURVIVE"


def test_scratch_replaced_by_link_is_not_followed(tmp_path, fake_designer):
    """Каталог запуска подменили ссылкой — очистка обязана отказаться, а не пройти по ней."""
    external = tmp_path / "external"
    (external / "вложенный").mkdir(parents=True)
    (external / "вложенный" / "precious.txt").write_bytes(b"MUST SURVIVE")
    root = tmp_path / "tmp" / "native-replaced"
    backend = _used_backend(root)
    shutil.rmtree(root)
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(root), str(external)],
                       check=True, capture_output=True)
    else:
        root.symlink_to(external, target_is_directory=True)

    backend.cleanup()

    assert (external / "вложенный" / "precious.txt").read_bytes() == b"MUST SURVIVE"
    if root.exists() or root.is_symlink():
        os.rmdir(root) if os.name == "nt" else root.unlink()


def test_designer_out_of_successful_run_is_not_kept(tmp_path, fake_designer):
    """Протоколы успешных запусков — не диагностика, а накопление."""
    root = tmp_path / "tmp" / "native-success"
    backend = _used_backend(root)

    produced = list((root / "designer-out").glob("*.log"))
    backend.cleanup()

    assert produced, "подмена обязана была создать протоколы"
    assert not root.exists()
    assert not any(path.exists() for path in produced)
