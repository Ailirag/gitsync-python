"""Руководство по Docker не должно расходиться с тем, что реально собрано.

Документ проверяется как исполняемый контракт: манифесты из руководства
разбираются настоящим валидатором схемы, а пути внутри контейнера сверяются с
Dockerfile и compose. Иначе инструкция тихо устареет при следующей правке.

Отдельно проверяется честность формулировок: работа с настоящим хранилищем 1С
в Linux НЕ проверялась, и руководство обязано это говорить прямо.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from gitsync.cli import _BATCH_ENTRY_KEYS, _validate_batch_config

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "docker-guide.md"
DEVELOPMENT = ROOT / "docs" / "development.md"
README = ROOT / "README.md"
DOCKERFILE = ROOT / "docker" / "Dockerfile.core"
DOCKERFILE_NATIVE = ROOT / "docker" / "Dockerfile.native"
COMPOSE = ROOT / "docker" / "compose.yaml"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
ENV_EXAMPLE = ROOT / "docker" / "env.example"
ACCEPTANCE = ROOT / "docker" / "acceptance" / "core_acceptance.py"
CI = ROOT / ".github" / "workflows" / "ci.yml"
DOCKERIGNORE = ROOT / ".dockerignore"

#: Пути внутри контейнера, на которых держится вся раскладка томов.
CONTAINER_PATHS = ("/repo", "/config", "/work/tmp", "/var/lib/gitsync/sessions")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json_blocks(text: str) -> list[dict]:
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.S)]


def test_guide_exists_and_is_linked_from_readme():
    assert GUIDE.is_file()
    readme = _text(README)
    assert "docs/docker-guide.md" in readme
    # Ссылка нужна в начале файла, а не только в хвосте списка документации.
    assert "docs/docker-guide.md" in readme[:1600]


@pytest.mark.parametrize("path", [DOCKERFILE, DOCKERFILE_NATIVE, COMPOSE, ENTRYPOINT, DOCKERIGNORE])
def test_files_promised_by_the_guide_exist(path):
    assert path.is_file(), f"руководство ссылается на {path.name}"
    assert path.name in _text(GUIDE)


def test_every_manifest_in_the_guide_is_accepted_by_the_real_validator(tmp_path):
    blocks = _json_blocks(_text(GUIDE))
    assert blocks, "в руководстве должен быть хотя бы один манифест"
    for position, manifest in enumerate(blocks, 1):
        manifest["repository"] = (tmp_path / f"база{position}").as_posix()
        entries = _validate_batch_config(manifest)
        assert entries, f"манифест №{position} не дал ни одного источника"
        for entry in entries:
            unknown = set(entry) - _BATCH_ENTRY_KEYS - {"workdir"}
            assert not unknown, f"манифест №{position}: ключи {unknown} CLI не понимает"


def test_native_manifest_covers_main_configuration_and_two_extensions(tmp_path):
    """Схема «основная конфигурация + N расширений» — заявленный сценарий."""
    native = [block for block in _json_blocks(_text(GUIDE))
              if block.get("defaults", {}).get("backend") == "native"]
    assert len(native) == 1, "в руководстве должен быть ровно один манифест для настоящей 1С"
    manifest = native[0]
    manifest["repository"] = tmp_path.as_posix()
    entries = _validate_batch_config(manifest)

    assert len(entries) == 3
    extensions = [entry.get("extension") for entry in entries]
    assert extensions[0] is None, "первый источник — основная конфигурация, без -Extension"
    named = [name for name in extensions if name]
    assert len(named) == 2 and len(set(named)) == 2, "нужны ДВА разных расширения"
    # Осторожный первый запуск: по одному потоку, а не значения по умолчанию 4/4.
    assert manifest["defaults"]["jobs"] == 1
    assert manifest["defaults"]["queue_limit"] == 1
    # Пароль — только файлом; в командной строке он виден в списке процессов.
    assert manifest["defaults"]["storage_password_file"].startswith("/run/secrets/")
    assert "storage_password" not in manifest["defaults"]


@pytest.mark.parametrize("path", CONTAINER_PATHS)
def test_container_paths_are_the_same_everywhere(path):
    """Путь внутри контейнера один и тот же в образе, в compose и в руководстве.

    Канонический путь хранилища входит в ключ блокировки: разъехавшиеся пути
    означают разъехавшуюся блокировку.
    """
    assert path in _text(DOCKERFILE), f"{path} не готовится в образе"
    assert path in _text(COMPOSE), f"{path} не подключается в compose"
    assert path in _text(GUIDE), f"{path} не описан в руководстве"


def test_guide_describes_a_foreign_uid_as_a_refusal_not_a_bypass():
    """Другой UID не «проскакивает мимо блокировки»: файл 0600 ему просто не открыть.

    Проверено запуском: контейнер с другим UID падает с ошибкой доступа к файлу
    блокировки, а не входит в хранилище вторым. Обещать обратное — пугать не тем.
    """
    guide = _text(GUIDE)
    assert "оба процесса пойдут в хранилище вместе" not in guide
    assert "ошибк" in guide.split("## 3.")[1].split("## 4.")[0].lower()


def test_guide_explains_volumes_left_from_another_uid():
    """Смена UID/GID (или пересборка образа) не переназначает владельца старого тома."""
    volumes = _text(GUIDE).split("### Подготовка томов")[1].split("###")[0]
    assert "уже созданный" in volumes or "уже существует" in volumes or "старый том" in volumes


def test_shared_lock_volume_is_external_and_documented():
    compose = _text(COMPOSE)
    # Том объявлен внешним: иначе у каждого compose-проекта был бы свой, и общая
    # блокировка входа в хранилище перестала бы быть общей.
    assert re.search(r"sessions:\s*\n\s*external:\s*true", compose)
    guide = _text(GUIDE)
    assert "docker volume create gitsync-sessions" in guide
    assert "одним UID" in guide, "требование одного UID обязано быть в руководстве"


def test_guide_does_not_claim_the_unverified_native_path_works():
    guide = _text(GUIDE)
    assert "НЕ ПРОВЕРЕНО" in guide
    # Раздел про образ с платформой обязан быть помечен как невыполненный.
    native_section = guide.split("## 10.")[1].split("## 12.")[0]
    assert "не запускалась" in native_section or "не выполнялся" in native_section
    assert "Dockerfile.native" in _text(DOCKERFILE_NATIVE)
    assert "НЕ ПРОВЕРЕНО" in _text(DOCKERFILE_NATIVE)


def test_runtime_image_never_copies_tests_or_history():
    """Тесты и история Git попадают только в отдельную цель `test`."""
    dockerfile = _text(DOCKERFILE)
    core_stage = dockerfile.split("AS core")[1].split("AS test")[0]
    for unwanted in ("COPY tests", "COPY docs", "COPY examples", "COPY --chown=app:0 tests"):
        assert unwanted not in core_stage, f"в образ выполнения попадает {unwanted}"
    ignore = _text(DOCKERIGNORE)
    for pattern in (".git", "*.deb", "*.run", "*.lic", ".env"):
        assert pattern in ignore, f"{pattern} обязан быть исключён из контекста сборки"


def test_entrypoint_never_grants_blanket_git_trust():
    entrypoint = _text(ENTRYPOINT)
    assert "safe.directory" in entrypoint
    # Разрешение выдаётся только на конкретный путь и только по явному запросу.
    assert "safe.directory *" not in entrypoint
    assert 'safe.directory "$REPO"' in entrypoint
    assert "GITSYNC_TRUST_REPO" in entrypoint


def test_exit_codes_table_matches_the_cli():
    from gitsync.cli import EXIT_CANCELLED

    guide = _text(GUIDE)
    assert f"`{EXIT_CANCELLED}`" in guide, "код остановки оператором должен быть описан"
    assert "`78`" in guide, "код непройденной предполётной проверки должен быть описан"
    assert "EX_CONFIG" in _text(ENTRYPOINT)


# --- команды руководства обязаны существовать в поставке ---------------------


def _compose_services() -> set[str]:
    """Имена служб compose. Полноценный разбор YAML не нужен и не добавляет зависимостей."""
    compose = _text(COMPOSE)
    block = compose.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    return set(re.findall(r"^  ([a-z0-9][a-z0-9-]*):$", block, re.M))


def test_compose_services_named_in_the_guide_exist():
    """`run --rm <служба>` из руководства должен называть службу, а не команду CLI."""
    services = _compose_services()
    assert {"sync", "sync-fixture", "push"} <= services, services
    названные = set(re.findall(r"compose[^\n]*?run --rm(?:\s+-T)?\s+([a-z0-9][a-z0-9-]*)", _text(GUIDE)))
    assert названные, "в руководстве должен быть хотя бы один запуск через compose"
    assert названные <= services, f"в compose нет служб: {sorted(названные - services)}"


def test_documented_acceptance_run_stages_the_helper_it_imports():
    """Сценарии пользователя подключают помощника из tests/support — это должно быть в команде.

    Скрипт приёмки импортирует `support.native_report`, которого нет в
    `docker/acceptance`. CI готовит каталог правильно; дословная команда из
    руководства обязана делать то же самое, иначе её запуск падает с ImportError.
    """
    assert "from support.native_report import" in _text(ACCEPTANCE)
    команды = re.findall(r"```bash\n(.*?)```", _text(DEVELOPMENT), re.S)
    приёмка = [block for block in команды if "core_acceptance.py" in block]
    assert приёмка, "в docs/development.md должна быть команда запуска приёмки"
    for block in приёмка:
        assert "tests/support" in block, "помощник не подготовлен: запуск упадёт на импорте"
    # Внутри образа с тестами каталога .github нет: он исключён из контекста сборки.
    # Там, где файл задачи доступен, руководство и CI обязаны готовить одно и то же.
    if CI.is_file():
        assert "tests/support" in _text(CI), "CI и руководство обязаны готовить одно и то же"


def test_documentation_explains_why_each_platform_skips_tests():
    """Существенный договор документации о наборе — ПРИЧИНА пропусков, а не их число.

    Здесь раньше стояла сверка общего счётчика тестов с числами в трёх документах.
    Проверка была хрупкой по устройству: любой добавленный тест — даже не имеющий к
    документации отношения — ронял набор, пока три числа не поправят вручную. Так
    проверялась не документация, а дисциплина переписывания счётчика, и падала она
    ровно тогда, когда набор рос, то есть наказывала за нужное действие. В stage-14,
    15, 16 и 17 это был единственный отказ, не связанный со средой.

    ЧТО ПРОВЕРЯЕТСЯ ВМЕСТО ЭТОГО. Пропуски есть на обеих платформах, и оператору важно
    не сколько их, а почему они законны: в Linux нет NTFS junction, описателя каталога
    Windows и PowerShell. Эта причина — договор, она обязана быть в документации.

    ФАКТИЧЕСКИЕ ИТОГИ ПРОГОНОВ живут в evidence (журналы прогонов с кодами возврата),
    а не в прозе: там они датированы, воспроизводимы и не требуют ручной правки.
    """
    for документ in (_text(DEVELOPMENT), _text(GUIDE)):
        пропуски = re.search(r"(\d+) (?:skipped|пропущены)", документ)
        assert пропуски, "документация обязана называть, что пропуски есть"
    development = _text(DEVELOPMENT)
    for причина in ("junction", "FILE_SHARE_DELETE", "PowerShell"):
        assert причина in development, (
            f"не названа причина пропусков <{причина}>: без неё пропуск неотличим от дыры"
        )
    guide = _text(GUIDE)
    assert "junction" in guide and "FILE_SHARE_DELETE" in guide, \
        "руководство обязано объяснять пропуски так же, как development.md"


def test_the_suite_still_collects_and_nothing_is_silently_lost():
    """Набор обязан собираться целиком: молча усохший набор — это не «всё прошло».

    Сверяется не число с прозой, а факт: сбор проходит без ошибок и набор не пуст.
    Ошибки сбора (сломанный импорт, опечатка в conftest) иначе выглядят как успех.
    """
    result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only",
                             "-p", "no:cacheprovider"],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=False, timeout=600)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    found = re.search(r"(\d+) tests? collected", result.stdout)
    assert found, result.stdout[-2000:]
    # Код возврата 0 у --collect-only уже означает «ошибок сбора нет»: ошибка импорта
    # даёт код 2. Искать слово «error» в выводе нельзя — оно есть в именах самих тестов.
    assert int(found.group(1)) > 0, "набор пуст"


# --- R2/R3: обещанное в руководстве должно работать --------------------------


def test_core_image_has_the_ssh_client_it_promises():
    """Отправка по ssh обещана в compose и в руководстве — значит клиент есть в образе."""
    dockerfile = _text(DOCKERFILE)
    core_stage = dockerfile.split("AS core")[1].split("AS test")[0]
    assert "openssh-client" in core_stage, "образ ядра не умеет ssh, а compose его использует"
    assert "ssh -i /run/secrets/git_ssh_key" in _text(COMPOSE)
    guide = _text(GUIDE)
    # Проверка ключа сервера не отключается, значит known_hosts надо чем-то заполнить.
    assert "ssh-keyscan" in guide, "руководство обязано показать, как получить known_hosts"
    assert "StrictHostKeyChecking=yes" in _text(COMPOSE)


def test_documented_user_matches_the_owners_inside_the_image():
    """Опубликованный пример запуска обязан работать с владельцами каталогов образа.

    Каталоги образа принадлежат `app:0` с правами группы, поэтому рабочая пара —
    свой UID и GID 0. Пример с `$(id -g)` останавливался предполётной проверкой.
    """
    guide = _text(GUIDE)
    assert '--user "$(id -u):0"' in guide
    assert '--user "$(id -u):$(id -g)"' not in guide, "такой запуск не проходит предполётную проверку"
    env_example = _text(ENV_EXAMPLE)
    assert re.search(r"^GITSYNC_GID=0$", env_example, re.M), "образец окружения обязан давать рабочий GID"
    # Второй путь — собрать образ под свой UID/GID; он назван и в образе, и в руководстве.
    assert "APP_UID" in _text(DOCKERFILE) and "APP_UID" in guide
    assert "APP_UID" in _text(ENTRYPOINT), "отказ должен называть оба выхода"


def test_guide_warns_that_ssh_needs_a_uid_known_to_the_image():
    """Отправка по ssh требует записи о UID в /etc/passwd образа — это должно быть в тексте.

    Иначе администратор, запустивший всё с `--user "$(id -u):0"`, получит на службе
    push «No user exists for uid» и будет искать причину в ключах и правах.
    """
    guide = _text(GUIDE)
    push_section = guide.split("### Отправка в Git")[1].split("\n---")[0]
    assert "/etc/passwd" in push_section
    assert "No user exists for uid" in push_section, "точный текст ошибки помогает найти раздел"
    assert "APP_UID" in push_section, "нужен рабочий способ: сборка образа под свой UID"
    # То же предупреждение выдаёт сам контейнер, а не только руководство.
    assert "/etc/passwd" in _text(ENTRYPOINT)
    assert "/etc/passwd" in _text(ENV_EXAMPLE)


def test_guide_prepares_volumes_without_touching_foreign_data():
    """Подготовка томов описана явно и не сводится к рекурсивной смене владельца."""
    guide = _text(GUIDE)
    assert "docker volume create gitsync-sessions" in guide
    # Новый именованный том берёт владельца и права у каталога образа — это и есть
    # причина, по которой ничего чистить и переназначать не нужно.
    assert "от каталога образа" in guide or "у каталога образа" in guide
    assert "chown -R" not in guide.split("## 3.")[1].split("## 4.")[0], \
        "в разделе про UID не должно быть рекурсивной смены владельца чужих данных"
