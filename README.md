# gitsync-py

Порт [oscript-library/gitsync](https://github.com/oscript-library/gitsync) на Python: выгрузка версий
хранилища конфигурации 1С:Предприятие в Git с сохранением автора, даты и комментария версии.
Отличие от оригинала — **параллельная выгрузка версий** (каждая версия в изолированной временной базе
и рабочем каталоге) при **строго последовательных коммитах** в порядке номеров версий.

Лицензия — MPL-2.0, как у оригинала (см. `LICENSE`).

## Установка

```bash
uv venv .venv
uv pip install --python .venv -e ".[dev]"
```

CLI после установки: `gitsync-py` (либо `python -m gitsync`).

## Быстрый старт

```bash
# 1. Создать рабочую копию и служебные файлы AUTHORS/VERSION
gitsync-py init --workdir ./repo --storage-path tcp://srv/storage --storage-user Иванов

# 2. Синхронизировать хранилище в Git (4 параллельных экспортёра)
gitsync-py sync --workdir ./repo --storage-path tcp://srv/storage \
    --storage-user Иванов --storage-password-env GITSYNC_STORAGE_PASSWORD \
    --jobs 4 --v8-version 8.3.24

# 3. Принудительно выставить синхронизированную версию
gitsync-py set-version --workdir ./repo --version 120
```

Пароль хранилища передаётся **только** через переменную окружения (`--storage-password-env`)
или файл (`--storage-password-file`). В логах и в сообщениях об ошибках пароль маскируется.

## Команды

| Команда | Назначение |
|---|---|
| `init` | создать/подготовить рабочую копию Git, сгенерировать `AUTHORS` и `VERSION` |
| `clone` | `init` + полная синхронизация с нуля |
| `sync` | догрузить новые версии хранилища в Git |
| `set-version` | записать номер синхронизированной версии в `VERSION` |
| `sync-all` | пакетная обработка нескольких хранилищ из конфигурационного файла |
| `plugins list` | показать загруженные Python-плагины |

Подробности и полный список опций: `gitsync-py --help`, `gitsync-py sync --help`.

## Документация

- [Матрица совместимости с upstream](docs/compatibility-matrix.md) — что перенесено, что нет, что не проверено.
- [Руководство по миграции с gitsync (OneScript)](docs/migration.md).
- [API плагинов](docs/plugins.md) и его границы.
- [Журнал разработки и команды тестов](docs/development.md).

## Ограничения этой итерации

Нативные вызовы конфигуратора (`/ConfigurationRepositoryReport`, `/ConfigurationRepositoryUpdateCfg`,
`/DumpConfigToFiles`) собраны по исходникам `v8runner`/gitsync, но **не проверены на живой платформе**
в рамках итерации 1. Для герметичных тестов есть явно помеченный файловый бэкенд
(`--backend fixture`), который читает заранее подготовленный отчёт и каталоги выгрузки.
