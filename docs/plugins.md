# Python-плагины: контракт parity-07

Это **адаптация семантики**, не исполнение OneScript и не полная совместимость всех внешних
плагинов upstream. Загружаются только явно указанные доверенные Python-модули через
`--plugin package.module`, с функцией `register(host)`. Автопоиска/установки/запуска `.os`,
`opm`, bindata, чужих Git hooks нет. Python-модуль — произвольный код с правами процесса,
не sandbox: нельзя загружать недоверенные модули. Safety checks защищают операции ядра,
но не могут запретить самому Python-коду писать файлы или запускать процессы.

```python
def register(host):
    def change(ctx):
        ctx.message = '[export] ' + ctx.message
        ctx.author = 'Integration <integration@example.org>'
    host.subscribe('before_commit', change, priority=100, contextual=True)
```

## Диспетчеризация

`subscribe(event, handler, priority=0, contextual=False)`. Приоритет **по убыванию**,
равные приоритеты — порядок регистрации. `contextual=True`: один изменяемый объект `ctx`
передаётся всем подписчикам события; изменения видны следующему подписчику и затем ядру
в перечисленных ниже изменяемых полях. Возвращаемое значение callback игнорируется.
Legacy `contextual=False` получает именованные аргументы прежних событий; mutable списки
работают как раньше. `PluginHost(strict=True)` по умолчанию: ошибки останавливают sync.
`strict=False` — явный старый режим предупреждений только для legacy callbacks;
ошибки contextual callbacks **всегда** fail closed.

Callbacks одного host сериализованы RLock. Export callbacks исполняются в worker threads,
но одновременно два callback одного host не исполняются. Сам backend остаётся параллельным.
Не ждать в callback другой callback/worker того же host: это может вызвать deadlock.
Порядок между различными версиями export не обещается; коммиты строго последовательные.
При retries export-события могут повторяться: требуются идемпотентность и отмена через `cancel`.

## События и реально используемые изменения

| Событие | Контекст | Эффект |
|---|---|---|
| `before_sync` | `work_dir` | После recovery под writer lock, до проверки чистоты/чтения marker. Подходит для явно выбранной remote-политики. Изменение поля пути не перенаправляет ядро. |
| `before_history` | `start`, `current_version`, `history=None`, `standard_processing=True` | Можно изменить start. False отменяет backend.fetch_history; необходимо предоставить history (список StorageVersion). |
| `after_history` | `history`, `current_version` | Можно заменить список и записи через dataclasses.replace; сортировка и проверка дублей выполняются после callback. Текущий marker этим полем не изменяется. |
| `before_export` | `version`, `destination`, `cancel`, `standard_processing=True` | False отменяет backend export; плагин обязан создать snapshot в выделенном destination. Переназначение destination/version не меняет выбранный ядром путь/номер. |
| `after_export` | `version`, `destination` | Можно преобразовать подготовленный snapshot. Затем обязательны подтверждение выгрузки и reserved/symlink validation. |
| `before_cleanup` | `version`, `work_dir`, `standard_processing=True` | False отменяет удаления отсутствующих в snapshot файлов (merge snapshot). Это изменение плана, **не** разрешение вручную чистить worktree. Перезапись пришедших файлов всё ещё выполняется транзакционно. |
| `before_commit` | `version`, `work_dir`, `author`, дополнительно `message`, `date` для contextual API | author/message/date реально передаются в Git. VERSION/номер, путь, records, expected HEAD, journal и index lock не подменяются. Прямые изменения файлов отклоняются checks транзакции. |
| `after_commit` | `version`, `work_dir`, `sha` | Только после durable commit. Ошибка → PostCommitError, ненулевой результат, commit и VERSION сохранены. |
| `after_sync` | `work_dir`, `result` | Только успешное завершение, в том числе no-op. Ошибка возвращается, уже сделанные commits не откатываются. Не является finally/cleanup callback. |

История может запрашиваться повторно с start=1 для проверки усечения storage при no-op.
Init генерирует AUTHORS обычным backend.fetch_history, lifecycle выше относится к sync.
Пустой/missing override snapshot подчиняется тому же allows_empty_export контракту backend.
Reserved-path validation повторяется перед построением транзакции; baseline проверяется
после cleanup callback. Флаг standard_processing не отключает safety/recovery gates.

## Соответствие источнику

`МенеджерПодписок.os:775–799` — убывающие приоритеты; `802–840` — возврат mutable параметров.
`МенеджерСинхронизации.os:264–357` — lifecycle, `382–425` — export,
`566–588` — стандартная очистка/override. Перенесены ключевые data-oriented stages;
не перенесены все 30+ событий, native-конфигуратор/IB contexts, произвольная замена Git commit,
низкоуровневых load/move, CLI subscription и установка/включение пакетов upstream.
Полная drop-in plugin parity **не заявляется**.

## Remote-политика: только opt-in

Без явно загруженного модуля sync не выполняет fetch/pull/push. `clone --url` — отдельная
явная операция. Источник upstream sync-remote — внешний проект, его точный контракт в
исследованном snapshot отсутствует; совместимость с ним не заявляется.

`tests/test_parity07_remote.py` исполняет настоящий Python-модуль с before_sync:
clean → fetch → merge --ff-only и after_commit: push. Только собственный local bare fixture,
без сети. Проверены remote advance, successful push, divergence refusal и pre-receive rejection.
При отклонённом push локальный commit остаётся; в тесте оператор явно повторяет push после
устранения отказа. Универсального установленного remote-плагина, автоматического retry push
на no-op, rebase, force push, произвольной branch/auth политики в продукте **нет**.
