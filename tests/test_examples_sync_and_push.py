"""Обёртка регламентного запуска: данные не должны становиться командами.

Имя ветки, имя удалённого репозитория и пути приходят в обёртку извне, причём
имя ветки она читает прямо из репозитория, а Git допускает в нём `&`, `%`, `|`,
`;` и кавычки. Пока обёртка склеивала из этих данных строку для `cmd /c`, такое
имя выполнялось как команда, а код возврата принадлежал последней выполненной
команде, а не `git push`: неудачная отправка выглядела успехом.

Проверяется настоящий запуск: настоящий Windows PowerShell 5.1, настоящий git,
локальный bare-репозиторий вместо удалённого. Подменяется ровно одно — сама
выгрузка (`-GitSync` указывает на заглушку): платформы 1С на машине разработки
нет, а предмет проверки — обёртка, а не выгрузка. Там, где важен запуск не
batch-файла, а настоящей программы, берётся установленный `gitsync-py`.

Отдельно проверяется уборка: префикс имени каталога не доказывает, что каталог
принадлежит инструменту, поэтому каталоги обёртка не удаляет вовсе.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "examples" / "sync-and-push.ps1"

#: Настоящие прогоны возможны только там, где есть Windows PowerShell 5.1.
windows_only = pytest.mark.skipif(os.name != "nt", reason="обёртка написана для Windows PowerShell 5.1")


# --- вспомогательное ---------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} -> {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _stub(path: Path, code: int = 0) -> Path:
    """Заглушка выгрузки: пишет в оба потока и возвращает заданный код.

    Только ASCII: содержимое читает cmd.exe в кодировке консоли, и кириллица тут
    добавила бы шума, никак не относящегося к предмету проверки.
    """
    path.write_text(
        "@echo off\r\n"
        "echo STUB-STDOUT sync-all\r\n"
        "echo STUB-STDERR journal line 1>&2\r\n"
        f"exit /b {code}\r\n",
        encoding="ascii", newline="",
    )
    return path


def _manifest(path: Path, repository: Path, *, bom: bool = False) -> Path:
    text = json.dumps({
        "repository": repository.as_posix(),
        "defaults": {"backend": "fixture"},
        "storages": [{"name": "Конфигурация", "subtree": "Конфигурация",
                      "fixture_root": (path.parent / "фикстура").as_posix()}],
    }, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8-sig" if bom else "utf-8", newline="\n")
    return path


def _repo(root: Path, branch: str) -> Path:
    """Рабочая копия с одним коммитом на ветке ``branch`` и bare-зеркалом `origin`."""
    repo = root / "репозиторий"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", branch, ".")
    _git(repo, "config", "user.name", "Тест")
    _git(repo, "config", "user.email", "test@example.org")
    (repo / "файл.txt").write_text("содержимое", encoding="utf-8")
    _git(repo, "add", "-A", ".")
    _git(repo, "commit", "-q", "-m", "первый коммит")
    return repo


def _bare(root: Path, repo: Path, *, remote: str = "origin", name: str = "зеркало.git") -> Path:
    bare = root / name
    _git(root, "init", "-q", "--bare", str(bare))
    _git(repo, "remote", "add", remote, str(bare))
    return bare


def _run(tmp_path: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell")
    assert powershell, "Windows PowerShell не найден"
    return subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WRAPPER), *args],
        cwd=str(cwd or tmp_path), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False, timeout=600,
    )


def _log_text(log_dir: Path) -> str:
    logs = sorted(log_dir.glob("sync-*.log"))
    assert logs, f"обёртка не завела журнал в {log_dir}"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in logs)


def _stand(tmp_path: Path, *, branch: str = "main", code: int = 0,
           remote: str = "origin", bom: bool = False) -> dict:
    """Готовая площадка: репозиторий, зеркало, манифест, заглушка, каталог логов."""
    repo = _repo(tmp_path, branch)
    bare = _bare(tmp_path, repo, remote=remote)
    log_dir = tmp_path / "логи"
    log_dir.mkdir()
    return {
        "repo": repo,
        "bare": bare,
        "log_dir": log_dir,
        "manifest": _manifest(tmp_path / "манифест.json", repo, bom=bom),
        "stub": _stub(tmp_path / "заглушка.cmd", code),
        "args": lambda extra=(): [
            "-Manifest", str(tmp_path / "манифест.json"),
            "-GitSync", str(tmp_path / "заглушка.cmd"),
            "-LogDir", str(log_dir),
            "-MinFreeGB", "0",
            *extra,
        ],
    }


# --- R1: данные не исполняются ------------------------------------------------


def test_wrapper_never_builds_a_command_line_for_the_shell():
    """Статический контракт: в обёртке не должно остаться склейки строки для cmd.exe."""
    text = WRAPPER.read_text(encoding="utf-8-sig")
    assert "cmd /c" not in text, "имя ветки снова попадёт в cmd.exe как команда"
    assert "cmd.exe /c" not in text
    # Аргументы передаются отдельными значениями, а не как часть одной строки.
    assert "Start-Process" in text


@windows_only
def test_push_of_a_branch_with_metacharacters_creates_the_exact_ref(tmp_path):
    """`&` и `%` в имени ветки — данные: отправляется ровно эта ссылка."""
    branch = "release&ver%PATH%"
    stand = _stand(tmp_path, branch=branch)
    result = _run(tmp_path, *stand["args"](["-Push"]))

    assert result.returncode == 0, result.stdout + result.stderr
    refs = _git(tmp_path, "--git-dir", str(stand["bare"]), "show-ref")
    head = _git(stand["repo"], "rev-parse", "HEAD").strip()
    assert f"{head} refs/heads/{branch}" in refs, refs
    # `ver` — встроенная команда cmd.exe: её баннер в журнале означал бы исполнение данных.
    assert "Microsoft Windows [Version" not in _log_text(stand["log_dir"])


@windows_only
def test_branch_name_never_runs_an_injected_command(tmp_path):
    """Имя ветки, создающее каталог при склейке, не создаёт ничего.

    `md;INJECTED` — команда cmd.exe без единого пробела (`;` у cmd.exe такой же
    разделитель, как пробел), а `review&md;INJECTED` — законное имя ветки Git.
    Под старой обёрткой это имя создавало каталог на диске.
    """
    branch = "review&md;INJECTED"
    stand = _stand(tmp_path, branch=branch)
    result = _run(tmp_path, *stand["args"](["-Push"]))

    следы = [path for path in tmp_path.rglob("INJECTED") if path.is_dir()]
    assert not следы, f"данные ветки выполнились как команда: {следы}"
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"refs/heads/{branch}" in _git(tmp_path, "--git-dir", str(stand["bare"]), "show-ref")


@windows_only
def test_remote_name_with_metacharacters_is_passed_as_data(tmp_path):
    stand = _stand(tmp_path, branch="main", remote="зеркало&ver")
    result = _run(tmp_path, *stand["args"](["-Push", "-Remote", "зеркало&ver"]))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "refs/heads/main" in _git(tmp_path, "--git-dir", str(stand["bare"]), "show-ref")


@windows_only
def test_failed_push_is_reported_as_failure(tmp_path):
    """Недоступное зеркало: код 5 и честная запись, а не «ИТОГ: УСПЕХ»."""
    stand = _stand(tmp_path, branch="main")
    shutil.rmtree(stand["bare"])
    result = _run(tmp_path, *stand["args"](["-Push"]))

    assert result.returncode == 5, result.stdout + result.stderr
    log = _log_text(stand["log_dir"])
    assert "ИТОГ: УСПЕХ" not in log
    assert "ОТПРАВКА НЕ УДАЛАСЬ" in log


@windows_only
def test_rejected_push_is_named_a_divergence(tmp_path):
    """Расхождение историй по-прежнему распознаётся по выводу git."""
    stand = _stand(tmp_path, branch="main")
    # В зеркале появляется чужой коммит: push станет non-fast-forward.
    _git(stand["repo"], "push", "-q", "origin", "main")
    другой = tmp_path / "другой"
    _git(tmp_path, "clone", "-q", str(stand["bare"]), str(другой))
    _git(другой, "config", "user.name", "Другой")
    _git(другой, "config", "user.email", "other@example.org")
    # HEAD у bare-зеркала показывает на master: ветку нужно взять явно.
    _git(другой, "checkout", "-q", "-B", "main", "origin/main")
    (другой / "чужой.txt").write_text("чужая работа", encoding="utf-8")
    _git(другой, "add", "-A", ".")
    _git(другой, "commit", "-q", "-m", "чужой коммит")
    _git(другой, "push", "-q", "origin", "main")
    (stand["repo"] / "свой.txt").write_text("своя работа", encoding="utf-8")
    _git(stand["repo"], "add", "-A", ".")
    _git(stand["repo"], "commit", "-q", "-m", "свой коммит")

    result = _run(tmp_path, *stand["args"](["-Push"]))

    assert result.returncode == 5, result.stdout + result.stderr
    assert "ОТПРАВКА ОТКЛОНЕНА" in _log_text(stand["log_dir"])


@windows_only
def test_export_failure_uses_the_child_exit_code(tmp_path):
    """Код возврата принадлежит запущенной программе, а не последней команде оболочки."""
    stand = _stand(tmp_path, branch="main", code=3)
    result = _run(tmp_path, *stand["args"](["-Push"]))

    assert result.returncode == 4, result.stdout + result.stderr
    log = _log_text(stand["log_dir"])
    assert "(код 3)" in log, log
    # Отправка не выполнялась: зеркало пустое.
    assert _git(tmp_path, "--git-dir", str(stand["bare"]), "show-ref", check=False).strip() == ""


@windows_only
def test_child_output_reaches_the_daily_log(tmp_path):
    """Оба потока программы попадают в суточный журнал, и он остаётся читаемым."""
    stand = _stand(tmp_path, branch="main")
    result = _run(tmp_path, *stand["args"]())

    assert result.returncode == 0, result.stdout + result.stderr
    log_path = stand["log_dir"] / f"sync-{time.strftime('%Y%m%d')}.log"
    raw = log_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "журнал не должен получать BOM в начале"
    assert b"\x00" not in raw, "журнал не должен превращаться в UTF-16"
    text = raw.decode("utf-8", errors="replace")
    assert "STUB-STDOUT sync-all" in text
    assert "STUB-STDERR journal line" in text
    assert "ИТОГ: УСПЕХ" in text
    # Свои строки обёртки остаются кириллицей, а не мусором.
    assert "ШАГ 1: выгрузка версий хранилищ в Git" in text


@windows_only
def test_paths_with_ampersand_and_spaces_are_passed_as_data(tmp_path):
    """Каталоги с `&` и пробелами — обычные пути, а не части команды."""
    площадка = tmp_path / "отдел R&D (тест)"
    площадка.mkdir()
    stand = _stand(площадка, branch="main")
    result = _run(tmp_path, *stand["args"](["-Push"]))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "refs/heads/main" in _git(tmp_path, "--git-dir", str(stand["bare"]), "show-ref")


@windows_only
def test_percent_in_a_path_with_a_batch_target_fails_closed(tmp_path):
    """`%` в пути и заглушка-`.cmd`: cmd.exe разобрал бы аргумент заново.

    Отказ до запуска честнее молчаливой подстановки переменной окружения.
    """
    площадка = tmp_path / "каталог %TEMP% внутри"
    площадка.mkdir()
    stand = _stand(площадка, branch="main")
    result = _run(tmp_path, *stand["args"]())

    assert result.returncode == 2, result.stdout + result.stderr
    log = _log_text(stand["log_dir"])
    assert "%" in log and ".cmd" in log.lower(), log


@windows_only
def test_percent_in_a_path_is_data_for_a_real_program(tmp_path):
    """Та же площадка с настоящей программой: `%` остаётся частью пути."""
    # Команда ставится рядом с python окружения; PATH при запуске pytest может её не содержать.
    рядом = Path(sys.executable).parent / "gitsync-py.exe"
    console_script = str(рядом) if рядом.is_file() else shutil.which("gitsync-py")
    if not console_script:
        pytest.skip("gitsync-py не установлен в это окружение")
    площадка = tmp_path / "каталог %TEMP% внутри"
    площадка.mkdir()
    repo = _repo(площадка, "main")
    фикстура = площадка / "фикстура"
    shutil.copytree(ROOT / "examples" / "fixture-storage", фикстура)
    log_dir = площадка / "логи"
    log_dir.mkdir()
    manifest = _manifest(площадка / "манифест.json", repo)

    result = _run(tmp_path, "-Manifest", str(manifest), "-GitSync", console_script,
                  "-LogDir", str(log_dir), "-MinFreeGB", "0")

    assert result.returncode == 0, result.stdout + result.stderr
    log = _log_text(log_dir)
    assert "Зафиксировано версий: 2" in log, log


@windows_only
def test_manifest_with_bom_is_accepted(tmp_path):
    """CLI принимает UTF-8 с BOM — обёртка не должна отвергать такой манифест сама."""
    stand = _stand(tmp_path, branch="main", bom=True)
    result = _run(tmp_path, *stand["args"]())

    assert result.returncode == 0, result.stdout + result.stderr


# --- R4: уборка не трогает чужого --------------------------------------------


def test_wrapper_never_removes_directories():
    """Статический контракт: рекурсивного удаления каталогов в обёртке нет."""
    text = WRAPPER.read_text(encoding="utf-8-sig")
    assert "-Recurse" not in text, "префикс имени не доказывает принадлежность каталога"
    assert "Remove-Item" in text, "свои старые журналы обёртка по-прежнему убирает"


@windows_only
def test_foreign_directories_in_temp_root_are_never_deleted(tmp_path):
    """Каталог с именем инструмента, созданный не инструментом, остаётся на месте."""
    stand = _stand(tmp_path, branch="main")
    temp_root = tmp_path / "временные"
    for name in ("run-unrelated", "native-unrelated"):
        foreign = temp_root / name
        foreign.mkdir(parents=True)
        (foreign / "важное.txt").write_text("чужие данные", encoding="utf-8")
        старое = time.time() - 40 * 86400
        os.utime(foreign, (старое, старое))

    result = _run(tmp_path, *stand["args"](["-TempRoot", str(temp_root)]))

    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("run-unrelated", "native-unrelated"):
        assert (temp_root / name / "важное.txt").is_file(), f"{name} удалён обёрткой"
    # Про найденные старые каталоги обёртка сообщает, но не удаляет их.
    log = _log_text(stand["log_dir"])
    assert "не удаляет" in log or "не удаляю" in log, log


@windows_only
def test_only_own_log_files_are_removed(tmp_path):
    """Убираются ровно свои журналы `sync-ГГГГММДД.log`, и ничего похожего."""
    stand = _stand(tmp_path, branch="main")
    log_dir = stand["log_dir"]
    свой = log_dir / "sync-20200101.log"
    чужие = [log_dir / "sync-отчёт.log", log_dir / "sync-2020010.log",
             log_dir / "sync-20200101.log.bak", log_dir / "важное.log"]
    старое = time.time() - 400 * 86400
    for path in [свой, *чужие]:
        path.write_text("старое содержимое", encoding="utf-8")
        os.utime(path, (старое, старое))

    result = _run(tmp_path, *stand["args"](["-KeepLogDays", "30"]))

    assert result.returncode == 0, result.stdout + result.stderr
    assert not свой.exists(), "свой старый журнал должен убираться"
    for path in чужие:
        assert path.is_file(), f"{path.name} обёртке не принадлежит"
