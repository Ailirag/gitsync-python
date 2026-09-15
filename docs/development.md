# Журнал разработки (итерация 1)

Здесь фиксируются реально выполненные команды и их фактический вывод. Ничего не додумано:
если прогон не выполнялся — так и написано.

## Окружение

* Windows 10 Pro 19045, PowerShell/Git Bash, git 2.55.0.windows.4.
* `uv 0.12.0`, изолированный venv `.venv` на CPython 3.11.3.
* Переменная `PYTHONPATH` принудительно очищается в каждой команде (`PYTHONPATH= ...`):
  в этой машине она указывает на сторонний агент и «протекает» в venv.
* Платформа 1С:Предприятие на машине разработки **отсутствует**, стенда нет →
  нативные прогоны конфигуратора не выполнялись (см. «Непроверенные швы»).

## Подготовка окружения

```bash
PYTHONPATH= uv venv --python 3.11 .venv
PYTHONPATH= uv pip install --python .venv -q pytest ruff hatchling
PYTHONPATH= uv pip install --python .venv -q -e .
```

## Ход TDD по слайсам (RED → GREEN)

| Слайс | Тесты | RED (до реализации) | GREEN (после) |
|---|---|---|---|
| 1. VERSION/AUTHORS/отчёт | `tests/test_metadata_files.py` | `ModuleNotFoundError: No module named 'gitsync'` | `9 passed in 0.04s` |
| 2. git + нативные argv | `tests/test_git_repo.py`, `tests/test_designer.py` | `ModuleNotFoundError: No module named 'gitsync.designer'` | `18 passed in 2.85s` |
| 3. параллельный конвейер | `tests/test_sync_pipeline.py` | `ModuleNotFoundError: No module named 'gitsync.backends'` | сначала `2 failed, 14 passed`, после правок (`safe_join`, арифметика дат в тесте) — `16 passed in 8.38s` |
| 4. CLI и пакетный режим | `tests/test_cli.py` | `ModuleNotFoundError: No module named 'gitsync.cli'` | `9 passed in 2.38s`, затем +1 тест на пример из `examples/` |

Промежуточный провал слайса 3 зафиксирован честно: тест на выход выгрузки за пределы каталога
не проходил, потому что запись «наружу» вообще не проверялась; это привело к появлению
`gitsync/safepath.py` и его использованию в бэкендах.

## Итоговый прогон

```bash
PYTHONPATH= ./.venv/Scripts/python.exe -m pytest
```

```
.....................................................                    [100%]
53 passed in 13.41s
```

Распределение (`pytest --collect-only -q`):

```
tests/test_cli.py: 10
tests/test_designer.py: 8
tests/test_git_repo.py: 10
tests/test_metadata_files.py: 9
tests/test_sync_pipeline.py: 16
```

## Статический анализ

```bash
PYTHONPATH= ./.venv/Scripts/python.exe -m ruff check --output-format=concise src tests
```

```
All checks passed!
```

## Сборка и установка пакета

```bash
PYTHONPATH= uv build --out-dir dist .
```

```
Successfully built dist\gitsync_py-0.1.0.tar.gz
Successfully built dist\gitsync_py-0.1.0-py3-none-any.whl
```

```bash
PYTHONPATH= uv venv --python 3.11 D:/tmp/gsp-smoke
PYTHONPATH= uv pip install --python D:/tmp/gsp-smoke dist/gitsync_py-0.1.0-py3-none-any.whl
```

## Дымовой прогон CLI (установленный wheel, чистый venv)

```bash
gitsync-py --help
gitsync-py sync --workdir "D:/tmp/gsp-smoke-run/копия" --backend fixture \
  --fixture-root "D:/tmp/gsp-smoke-run/фикстура" --jobs 2 --email-domain example.org
```

Фактический вывод:

```
2026-09-15 12:38:44,210 INFO gitsync.sync: Синхронизированная версия: 0, максимум в хранилище: 2
2026-09-15 12:38:44,363 INFO gitsync.sync: Версия 1 зафиксирована (47d13459...)
2026-09-15 12:38:44,512 INFO gitsync.sync: Версия 2 зафиксирована (f65f15f3...)
Зафиксировано версий: 2 (1..2)
```

Проверка результата в репозитории:

```
git log --format='%an|%ae|%ad|%s' --date=format:'%Y-%m-%d %H:%M:%S' --reverse
Иванов|Иванов@example.org|2026-09-15 10:20:30|Первая версия
Петров|Петров@example.org|2026-09-16 08:00:00|Вторая версия

git rev-list --count HEAD  -> 2
```

Повторный запуск той же команды:

```
2026-09-15 12:38:44,756 INFO gitsync.sync: Синхронизированная версия: 2, максимум в хранилище: 0
Новых версий нет
```

Дублирующих коммитов не появилось (`rev-list --count HEAD` остался равен 2).
Временные каталоги дымового прогона после проверки удалены.

## Что проверяется тестами на настоящем git

* коммиты строго по возрастанию версии при экспорте, завершающемся вразнобой
  (`test_sync_commits_in_version_order_despite_out_of_order_export`);
* автор/почта/дата/комментарий и файл `VERSION` (`test_sync_preserves_author_date_and_version_marker`);
* переопределение подписи через `AUTHORS`;
* ограниченность конвейера (`--jobs`, `--queue-limit`) и изоляция каталогов на версию;
* сбой версии N не коммитит N+1, `VERSION` остаётся на последней успешной;
* повторы при временных сбоях и продолжение после обрыва без дублей коммитов;
* удаление объектов между версиями отражается в git (`git ls-files`, `git show --stat`);
* служебные файлы (`.git`, `.gitignore`, `.gitattributes`, `AUTHORS`, `VERSION`) переживают очистку;
* отмена укладывается в ограниченное время (тест требует < 6 с при 40 версиях по 0.25 с);
* грязная рабочая копия не трогается и вызывает отказ;
* пути с пробелами и кириллицей — во всех тестах рабочая копия называется «рабочая копия»;
* выход выгрузки за каталог и символьные ссылки отвергаются;
* блокировка цели: второй держатель получает `LockBusyError`, потоки сериализуются;
* argv конфигуратора: порядок `-v` после `ConfigurationRepositoryUpdateCfg`, отсутствие shell,
  маскирование паролей, таймаут.

## Непроверенные швы (честно)

1. **Нативный бэкенд не исполнялся.** `NativeStorageBackend` и `DesignerRunner` покрыты только
   тестами на состав argv. Ни один вызов 1С:Предприятие не выполнялся — платформы на машине нет.
2. **Формат отчёта `/ConfigurationRepositoryReport` не сверялся с живым отчётом.** Парсер написан
   по структуре русскоязычного отчёта; исходники `v8storage`, которыми пользуется upstream,
   недоступны. Фикстуры синтетические.
3. **Создание временной ИБ** (`CREATEINFOBASE File=...`) не проверялось; для реальных прогонов
   предусмотрен параметр `ib_factory`, чтобы подставить готовые базы.
4. **Ожидание лицензии 1С** (повтор при «Не обнаружено свободной лицензии!») не перенесено —
   есть только общий механизм повторов.
5. **Многопроцессная блокировка** проверена в пределах одного процесса (потоки) и логикой
   атомарного создания файла; кросс-процессный тест не писался.
6. **Симлинки на Windows**: тест допускает обе ветки, так как создание симлинка требует прав;
   на Linux ветка с реальным симлинком выполняется.
7. **CI не запускался** — workflow `.github/workflows/ci.yml` добавлен, но GitHub-репозиторий
   ещё не создан (нет авторизации), прогонов не было.
