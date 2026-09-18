"""Приёмка образа ЯДРА: сценарии пользователя через установленный пакет.

Запускается ВНУТРИ контейнера и обращается к продукту только так, как это делает
администратор: командой ``gitsync-py`` из PATH. Исходники продукта скрипту не нужны
и намеренно не подключаются — проверяется именно установленное колесо.

Из репозитория подключается один тестовый помощник (``support.native_report``):
им собираются ВХОДНЫЕ данные — отчёт по версиям в формате MOXCEL. Продукт этим
помощником не пользуется.

Платформы 1С здесь нет: источник версий — файловая фикстура (``--backend fixture``).
Это проверка установки, прав, блокировок, сигналов и работы с настоящим Git,
а НЕ доказательство работы с настоящим хранилищем конфигурации.

Запуск::

    python /acceptance/core_acceptance.py /tmp/приёмка
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/acceptance")
from support.native_report import ReportVersion, build_report_mxl  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: {detail}")
        FAILURES.append(f"{name}: {detail}")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=300, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} -> {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def cli(*args: str, timeout: float = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gitsync-py", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False)


def storage(root: Path, rows: list[tuple[int, str, str, str, dict[str, str]]]) -> Path:
    """Фикстура хранилища: отчёт по версиям и каталоги ``v<N>``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.mxl").write_bytes(build_report_mxl([
        ReportVersion(number=number, author=author, date="16.09.2026", time=moment,
                      comment=comment)
        for number, author, moment, comment, _ in rows
    ]))
    for number, _, _, _, files in rows:
        for relative, text in files.items():
            target = root / f"v{number}" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
    return root


def simple_rows(count: int, author: str = "Иванов") -> list:
    return [(number, author, f"10:{number:02d}:00", f"версия {number}",
             {"Справочники/Товары.xml": f'<Товары версия="{number}"/>'})
            for number in range(1, count + 1)]


def manifest(path: Path, repo: Path, sources: dict[str, Path], temp: Path, **defaults) -> Path:
    config = {
        "repository": str(repo),
        "defaults": {"backend": "fixture", "init": True, "jobs": 2, "queue_limit": 2,
                     "email_domain": "example.org", "temp_root": str(temp), **defaults},
        "storages": [{"name": name, "subtree": name, "fixture_root": str(root)}
                     for name, root in sources.items()],
    }
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def commits(repo: Path) -> list[str]:
    return git(repo, "rev-list", "--reverse", "HEAD").split()


def touched(repo: Path, sha: str) -> list[str]:
    raw = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha)
    return [name for name in raw.split("\0") if name]


# --- 0. Установленное колесо -------------------------------------------------

def scenario_installed_wheel() -> None:
    print("S0: продукт взят из установленного колеса")
    probe = subprocess.run(
        [sys.executable, "-c",
         "import gitsync, hashlib;"
         "from importlib.resources import files;"
         "cfe = files('gitsync').joinpath('data/tempExtension.cfe').read_bytes();"
         "print(gitsync.__file__);"
         "print(len(cfe));"
         "print(hashlib.sha256(cfe).hexdigest())"],
        capture_output=True, text=True, timeout=120, check=False)
    check("модуль gitsync импортируется", probe.returncode == 0, probe.stderr.strip())
    if probe.returncode != 0:
        return
    location, size, digest = probe.stdout.split()
    check("gitsync установлен в site-packages, а не взят из исходников",
          "site-packages" in location, location)
    check("загрузочное расширение входит в пакет: размер", size == "3720", size)
    check("загрузочное расширение входит в пакет: sha256",
          digest == "3d4816246e24aa41f4870ae70ac7cc3fdfcdce6e6faf637ac1fcb9af46217102", digest)
    version = cli("--version")
    check("gitsync-py --version отвечает", version.returncode == 0, version.stderr.strip())


# --- 1. Ноль, одна, две и пять версий ----------------------------------------

def scenario_version_counts(base: Path) -> tuple[Path, dict[str, Path], Path]:
    print("S1: источники на 0, 1, 2 и 5 версий в одном репозитории")
    repo = base / "база"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "acceptance@example.org")
    git(repo, "config", "user.name", "Приёмка")
    (repo / "README.md").write_text("# общий репозиторий базы\n", encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "чужое исходное содержимое")

    sources = {name: storage(base / "хранилища" / name, simple_rows(count))
               for name, count in (("Пустое", 0), ("Одна", 1), ("Две", 2), ("Пять", 5))}
    config = manifest(base / "манифест.json", repo, sources, base / "врем")

    result = cli("sync-all", "--config", str(config))
    check("sync-all завершился успешно", result.returncode == 0,
          f"код {result.returncode}: {result.stderr.strip()[:500]}")
    check("источник без версий сообщает, что новых версий нет",
          "Новых версий нет" in result.stdout, result.stdout[-400:])

    all_commits = commits(repo)
    check("всего коммитов = чужой + 0 + 1 + 2 + 5", len(all_commits) == 1 + 8,
          f"получено {len(all_commits)}")
    for name, count in (("Пустое", 0), ("Одна", 1), ("Две", 2), ("Пять", 5)):
        own = [sha for sha in all_commits if any(f.startswith(name + "/") for f in touched(repo, sha))]
        check(f"источник <{name}>: коммитов {count}", len(own) == count, f"получено {len(own)}")
        if count:
            version = (repo / name / "VERSION").read_text(encoding="utf-8")
            check(f"источник <{name}>: VERSION = {count}",
                  f"<VERSION>{count}</VERSION>" in version, version.strip())

    # Порядок версий внутри источника — строго по возрастанию номера.
    five = [sha for sha in all_commits
            if any(f.startswith("Пять/") for f in touched(repo, sha))]
    messages = [git(repo, "log", "-1", "--format=%B", sha).strip() for sha in five]
    check("версии зафиксированы по порядку",
          messages == [f"версия {n}" for n in range(1, 6)], str(messages))
    check("вложенных .git не появилось",
          [p.relative_to(repo).as_posix() for p in repo.rglob(".git")] == [".git"],
          str(sorted(p.as_posix() for p in repo.rglob(".git"))))
    check("чужой файл в общем репозитории не тронут",
          (repo / "README.md").read_text(encoding="utf-8") == "# общий репозиторий базы\n")
    return repo, sources, config


# --- 2. Автор, дата, комментарий ---------------------------------------------

def scenario_metadata(base: Path) -> None:
    print("S2: автор, дата и комментарий версии переносятся в коммит")
    repo = base / "мета"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    rows = [
        (1, "Иванов", "10:20:30", "Создание хранилища конфигурации",
         {"Справочники/Товары.xml": '<Товары версия="1"/>'}),
        (2, "Петрова", "11:05:00", "Доработка справочника\nвторая строка комментария",
         {"Справочники/Товары.xml": '<Товары версия="2"/>'}),
    ]
    source = storage(base / "хранилище-мета", rows)
    config = manifest(base / "мета.json", repo, {"Конфигурация": source}, base / "врем-мета")
    (repo / "Конфигурация").mkdir(parents=True)
    (repo / "Конфигурация" / "AUTHORS").write_text(
        "Иванов=Иван Иванов <ivanov@corp.example>\n", encoding="utf-8", newline="\n")

    result = cli("sync-all", "--config", str(config))
    check("sync-all завершился успешно", result.returncode == 0, result.stderr.strip()[:500])

    rows_out = git(repo, "log", "--reverse", "--date=format:%Y-%m-%d %H:%M:%S",
                   "--format=%an%x1f%ae%x1f%ad%x1f%B%x1e").split("\x1e")
    parsed = [chunk.strip("\n").split("\x1f") for chunk in rows_out if chunk.strip()]
    # Первый коммит источника — служебный (VERSION/AUTHORS), далее версии.
    named = [row for row in parsed if row[0] in {"Иван Иванов", "Петрова"}]
    check("подпись автора взята из AUTHORS источника",
          any(row[0] == "Иван Иванов" and row[1] == "ivanov@corp.example" for row in named),
          str(named))
    check("автор без записи в AUTHORS получает домен из настроек",
          any(row[0] == "Петрова" and row[1].endswith("@example.org") for row in named),
          str(named))
    check("дата версии перенесена как есть",
          any(row[2] == "2026-09-16 10:20:30" for row in named), str([r[2] for r in named]))
    check("многострочный комментарий сохранён полностью",
          any(row[3].strip() == "Доработка справочника\nвторая строка комментария"
              for row in named), str([r[3] for r in named]))
    check("кириллица в комментарии не испорчена",
          all("?" not in row[3] for row in named), str([r[3] for r in named]))


# --- 3. Повтор без изменений и догрузка --------------------------------------

def scenario_noop_and_increment(repo: Path, sources: dict[str, Path], config: Path) -> None:
    print("S3: повторный запуск ничего не меняет, догрузка трогает только свой источник")
    before = commits(repo)
    trees_before = {name: git(repo, "rev-parse", f"HEAD:{name}").strip()
                    for name in ("Одна", "Две", "Пять")}

    repeat = cli("sync-all", "--config", str(config))
    check("повторный запуск успешен", repeat.returncode == 0, repeat.stderr.strip()[:500])
    check("повторный запуск не добавил ни одного коммита", commits(repo) == before,
          f"{len(before)} -> {len(commits(repo))}")

    storage(sources["Две"], simple_rows(3))
    grow = cli("sync-all", "--config", str(config))
    check("догрузка успешна", grow.returncode == 0, grow.stderr.strip()[:500])
    after = commits(repo)
    check("добавился ровно один коммит", len(after) == len(before) + 1,
          f"{len(before)} -> {len(after)}")
    check("изменился только свой подкаталог",
          all(name.startswith("Две/") for name in touched(repo, after[-1])),
          str(touched(repo, after[-1])))
    for name in ("Одна", "Пять"):
        check(f"дерево источника <{name}> не изменилось побайтово",
              git(repo, "rev-parse", f"HEAD:{name}").strip() == trees_before[name])
    check("история прежних коммитов не переписана", after[:len(before)] == before)


# --- 4. Блокировка репозитория ------------------------------------------------

def scenario_repository_lock(repo: Path, config: Path) -> None:
    print("S4: занятый репозиторий отвергается понятной ошибкой")
    gitdir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
    lock_file = gitdir / "gitsync-py.lock"
    handle = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = cli("sync-all", "--config", str(config), timeout=300)
        check("занятый репозиторий даёт ненулевой код возврата", busy.returncode != 0,
              f"код {busy.returncode}")
        combined = busy.stdout + busy.stderr
        check("в сообщении сказано, что цель уже обрабатывается",
              "уже обрабатывается" in combined, combined[-500:])
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)

    freed = cli("sync-all", "--config", str(config))
    check("после освобождения блокировки запуск проходит", freed.returncode == 0,
          freed.stderr.strip()[:500])


# --- 5. Сигналы и коды возврата ----------------------------------------------

def scenario_signal_and_exit_codes(base: Path) -> None:
    print("S5: остановка по SIGTERM и коды возврата")
    repo = base / "сигнал"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    source = storage(base / "хранилище-сигнал", simple_rows(200))
    config = manifest(base / "сигнал.json", repo, {"Источник": source},
                      base / "врем-сигнал", jobs=1, queue_limit=1)

    child = subprocess.Popen(["gitsync-py", "sync-all", "--config", str(config)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace")
    deadline = time.monotonic() + 60
    started = 0
    while time.monotonic() < deadline:
        try:
            started = len(commits(repo))
        except AssertionError:
            started = 0
        if started >= 5:
            break
        time.sleep(0.05)
    check("синхронизация действительно начала фиксировать версии", started >= 5,
          f"коммитов к моменту сигнала: {started}")
    child.send_signal(signal.SIGTERM)
    out, err = child.communicate(timeout=300)
    check("остановка по сигналу даёт код 130", child.returncode == 130,
          f"код {child.returncode}: {err.strip()[:400]}")
    check("сообщение об остановке напечатано", "останов" in (out + err).lower(), (out + err)[-300:])
    total = len(commits(repo))
    check("остановка произошла до конца очереди", total < 200, f"коммитов {total}")
    # После остановки не должно остаться ни недописанной выгрузки, ни правок в
    # отслеживаемых файлах. AUTHORS сюда не относится: это служебная таблица
    # подписей, её ведёт администратор, и продукт её намеренно не коммитит
    # (так же и после полностью успешного прогона).
    status = [line for line in
              git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
              if line.strip() and "AUTHORS" not in line]
    check("после остановки нет ни правок, ни недописанной выгрузки",
          status == [], str(status)[:400])

    resumed = cli("sync-all", "--config", str(config))
    check("продолжение после остановки завершает работу", resumed.returncode == 0,
          resumed.stderr.strip()[:500])
    check("после продолжения зафиксированы все версии", len(commits(repo)) == 200,
          f"коммитов {len(commits(repo))}")

    broken = base / "битый.json"
    broken.write_text("{ это не json", encoding="utf-8")
    check("нечитаемый манифест даёт код 2", cli("sync-all", "--config", str(broken)).returncode == 2)
    absent = cli("sync", "--workdir", str(base / "нет-такого"), "--backend", "fixture",
                 "--fixture-root", str(base / "нет-фикстуры"))
    check("отсутствующая фикстура даёт ненулевой код", absent.returncode != 0)
    check("справка доступна", cli("--help").returncode == 0)


def main() -> int:
    base = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/приёмка")
    base.mkdir(parents=True, exist_ok=True)
    print(f"Приёмка образа ядра, рабочий каталог: {base}")
    print(f"uid={os.getuid()} gid={os.getgid()} HOME={os.environ.get('HOME')} "
          f"TZ={os.environ.get('TZ')} sessions={os.environ.get('GITSYNC_SESSION_DIR')}")
    scenario_installed_wheel()
    repo, sources, config = scenario_version_counts(base)
    scenario_metadata(base)
    scenario_noop_and_increment(repo, sources, config)
    scenario_repository_lock(repo, config)
    scenario_signal_and_exit_codes(base)
    print(f"\nвсего проверок: {CHECKS}, неуспешных: {len(FAILURES)}")
    for item in FAILURES:
        print(f"  НЕ ПРОЙДЕНО: {item}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
