"""Сериализация входа в хранилище между РАЗНЫМИ контейнерами.

Что проверяется: два контейнера, запущенные с ОДНИМ UID, с общим томом блокировок
и с одинаковым каноническим путём хранилища, не входят в хранилище одновременно.
Именно для этого нужен общий том: у каждого контейнера свой HOME, и без общего
каталога блокировок оба сочли бы себя единственными.

ГРАНИЦА МОДЕЛИРОВАНИЯ. Настоящий конфигуратор 1С здесь не запускается: подменён
последний шов перед запуском процесса — ``DesignerRunner.run``. Подменённый
конфигуратор отмечает момент входа, удерживает сессию заданное время и отмечает
выход. Всё, что выше шва (ключ блокировки, общий том, порядок захвата), — настоящий
код продукта из установленного колеса. Проверка на живой платформе 1С остаётся
отдельным, ещё не выполненным пунктом приёмки.

Режимы::

    python session_probe.py --role a --observe /observe --storage /storage/main --hold 3
    python session_probe.py --check /observe
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

from gitsync.backends import NativeStorageBackend
from gitsync.designer import DesignerRunner, StorageAccess
from gitsync.repository_session import repository_session_path


class RecordingDesigner(DesignerRunner):
    """Шов вместо конфигуратора: отмечает окно удержания сессии хранилища."""

    def __init__(self, *args, observe: Path, role: str, hold: float, **kwargs):
        super().__init__(*args, **kwargs)
        self.observe = observe
        self.role = role
        self.hold = hold

    def run(self, args, timeout=None):  # noqa: ARG002
        if "/ConfigurationRepositoryDumpCfg" in args:
            start = time.time()
            time.sleep(self.hold)
            target = Path(args[args.index("/ConfigurationRepositoryDumpCfg") + 1])
            target.write_bytes(b"cf")
            (self.observe / f"{self.role}.json").write_text(json.dumps({
                "role": self.role, "start": start, "end": time.time(),
                "pid": os.getpid(), "host": socket.gethostname(), "uid": os.getuid(),
            }), encoding="utf-8")
        elif "/DumpConfigToFiles" in args:
            dest = Path(args[args.index("/DumpConfigToFiles") + 1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "Configuration.xml").write_text("<x/>", encoding="utf-8")
        return None


def probe(role: str, observe: Path, storage: str, hold: float, work: Path) -> int:
    observe.mkdir(parents=True, exist_ok=True)
    access = StorageAccess(path=storage, user="gitsync")
    session = repository_session_path(access)
    print(f"[{role}] uid={os.getuid()} host={socket.gethostname()} "
          f"session={session} sessions_dir={os.environ.get('GITSYNC_SESSION_DIR')}")
    runner = RecordingDesigner("не-запускается", work / "out", timeout=600,
                               observe=observe, role=role, hold=hold)
    backend = NativeStorageBackend(access, runner, work / "workers",
                                   ib_factory=lambda root: f"/F{root}/ib")
    backend.export_version(1, work / "xml")
    backend.cleanup()
    print(f"[{role}] завершено")
    return 0


def check(observe: Path) -> int:
    windows = []
    for item in sorted(observe.glob("*.json")):
        windows.append(json.loads(item.read_text(encoding="utf-8")))
    print(json.dumps(windows, ensure_ascii=False, indent=2))
    if len(windows) != 2:
        print(f"ОШИБКА: ожидались два окна, найдено {len(windows)}")
        return 1
    first, second = sorted(windows, key=lambda row: row["start"])
    overlap = min(first["end"], second["end"]) - max(first["start"], second["start"])
    hosts = {row["host"] for row in windows}
    uids = {row["uid"] for row in windows}
    print(f"контейнеров: {len(hosts)}, uid: {uids}, пересечение окон: {overlap:.3f} с")
    if len(hosts) != 2:
        print("ОШИБКА: оба окна пришли из одного контейнера — проверка бессмысленна")
        return 1
    if len(uids) != 1:
        print("ОШИБКА: контейнеры работали под разными UID — общий файл блокировки невозможен")
        return 1
    if overlap > 0:
        print(f"ОШИБКА: сессии хранилища пересеклись на {overlap:.3f} с")
        return 1
    print("СЕРИАЛИЗОВАНО: второй контейнер вошёл в хранилище только после первого")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role")
    parser.add_argument("--observe", required=True)
    parser.add_argument("--storage", default="/storage/main")
    parser.add_argument("--hold", type=float, default=3.0)
    parser.add_argument("--work", default="/tmp/probe")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        return check(Path(args.observe))
    return probe(args.role, Path(args.observe), args.storage, args.hold, Path(args.work))


if __name__ == "__main__":
    raise SystemExit(main())
