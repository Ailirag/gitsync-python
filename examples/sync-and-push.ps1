<#
.SYNOPSIS
    Один регулярный запуск: синхронизация хранилищ 1С в Git и (по желанию) отправка в удалённый репозиторий.

.DESCRIPTION
    Обёртка вокруг `gitsync-py sync-all`. Ничего не решает за инструмент: не трогает пароли,
    не делает force-push, не чинит расхождения и не удаляет каталоги — ни свои, ни чужие.

    Что делает:
      1. не даёт двум запускам идти одновременно (свой файл блокировки);
      2. проверяет свободное место на дисках репозитория и временных файлов;
      3. запускает `sync-all` и пишет весь вывод в суточный лог;
      4. только при успешной синхронизации и только с ключом -Push выполняет `git push`;
      5. убирает свои старые логи и показывает, что осталось от прежних запусков инструмента.

    Данные командами не становятся. Имя ветки обёртка читает из репозитория, а Git разрешает
    в нём `&`, `%`, `;`, `|` и кавычки; имя удалённого репозитория и пути задаёт администратор.
    Поэтому программы запускаются отдельными аргументами через CreateProcess, командный
    интерпретатор в цепочке не участвует, а код возврата берётся у самой программы, а не у
    последней команды строки.

    Каталоги обёртка не удаляет. Имя вида `run-*` не доказывает, что каталог создал инструмент
    и что в нём не идёт чужая работа: в общем каталоге временных файлов это удалило бы чужое.
    Удаляются только собственные журналы — файлы строго вида `sync-ГГГГММДД.log`.

    Коды возврата (их видно в планировщике как «результат последнего запуска»):
      0 — всё получилось;
      2 — ошибка в параметрах или путях;
      3 — мало свободного места, синхронизация не запускалась;
      4 — синхронизация не удалась (отправка не выполнялась);
      5 — синхронизация прошла, а отправка в удалённый репозиторий не удалась;
      6 — предыдущий запуск ещё не закончился.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File D:\gitsync\sync-and-push.ps1 `
        -Manifest D:\gitsync\config\base.json `
        -GitSync D:\gitsync\program\venv\Scripts\gitsync-py.exe `
        -LogDir D:\gitsync\logs -Push
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Manifest,
    [Parameter(Mandatory = $true)] [string] $GitSync,
    [string] $Repository = '',
    [switch] $Push,
    [string] $Remote = 'origin',
    [string] $Branch = '',
    [Parameter(Mandatory = $true)] [string] $LogDir,
    [int] $KeepLogDays = 30,
    [string] $TempRoot = '',
    [int] $StaleTempDays = 7,
    [double] $MinFreeGB = 5
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Вывод инструмента — UTF-8, иначе в логе вместо русских сообщений будут вопросы.
$env:PYTHONUTF8 = '1'
# На некоторых машинах PYTHONPATH задан глобально и «протекает» в venv: гасим только для этого процесса.
$env:PYTHONPATH = ''

$utf8 = New-Object System.Text.UTF8Encoding($false)
$дата_лога = Get-Date -Format 'yyyyMMdd'
$log = Join-Path $LogDir ('sync-{0}.log' -f $дата_лога)
#: Собственные файлы обёртки в каталоге логов — только эти два вида имён.
$свои_логи = '^sync-\d{8}(-\d-(out|err))?\.log$'

function Запись([string]$текст) {
    $строка = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $текст
    Write-Host $строка
    [System.IO.File]::AppendAllText($log, $строка + [Environment]::NewLine, $utf8)
}

function Аргумент([string]$значение) {
    # Одно значение — один аргумент. Правила те же, по которым программы Windows разбирают
    # командную строку (CommandLineToArgvW): значение целиком в кавычках, обратные слэши перед
    # кавычкой удваиваются. Интерпретатора команд в цепочке нет, поэтому `&`, `|`, `%`
    # и точка с запятой остаются обычными символами.
    $буфер = New-Object System.Text.StringBuilder
    [void]$буфер.Append('"')
    $слэши = 0
    foreach ($символ in $значение.ToCharArray()) {
        if ($символ -eq [char]'\') { $слэши++; continue }
        if ($символ -eq [char]'"') {
            [void]$буфер.Append('\' * ($слэши * 2 + 1))
            [void]$буфер.Append('"')
            $слэши = 0
            continue
        }
        if ($слэши -gt 0) { [void]$буфер.Append('\' * $слэши); $слэши = 0 }
        [void]$буфер.Append($символ)
    }
    [void]$буфер.Append('\' * ($слэши * 2))
    [void]$буфер.Append('"')
    return $буфер.ToString()
}

function Прочитать_файл([string]$путь) {
    if (-not (Test-Path -LiteralPath $путь)) { return '' }
    $байты = [System.IO.File]::ReadAllBytes($путь)
    if ($байты.Length -eq 0) { return '' }
    return [System.Text.Encoding]::UTF8.GetString($байты)
}

function Перенести_в_журнал([string]$путь) {
    # Вывод программы переносится в суточный лог как есть, байт в байт: перекодировать чужой
    # поток означало бы испортить то, что потом читает администратор.
    if (-not (Test-Path -LiteralPath $путь)) { return }
    $байты = [System.IO.File]::ReadAllBytes($путь)
    if ($байты.Length -gt 0) {
        $поток = [System.IO.File]::Open($log, [System.IO.FileMode]::Append,
            [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
        try { $поток.Write($байты, 0, $байты.Length) } finally { $поток.Dispose() }
    }
    Remove-Item -LiteralPath $путь -Force -ErrorAction SilentlyContinue
}

function Выполнить {
    <#
        Запускает программу отдельными аргументами и возвращает её настоящий код возврата
        вместе с собственным выводом. Потоки программы уходят прямо в файлы: Windows
        PowerShell 5.1 при слиянии потоков средствами оболочки обрывает запуск на первой
        строке чужого журнала, а его собственное перенаправление пишет файл в UTF-16.
    #>
    param(
        [Parameter(Mandatory = $true)] [string] $Программа,
        [string[]] $Аргументы = @(),
        [Parameter(Mandatory = $true)] [int] $Шаг,
        [switch] $ТихийЖурнал
    )
    $вывод = Join-Path $LogDir ('sync-{0}-{1}-out.log' -f $дата_лога, $Шаг)
    $ошибки = Join-Path $LogDir ('sync-{0}-{1}-err.log' -f $дата_лога, $Шаг)
    Remove-Item -LiteralPath $вывод -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ошибки -Force -ErrorAction SilentlyContinue

    $параметры = @{
        FilePath               = $Программа
        NoNewWindow            = $true
        Wait                   = $true
        PassThru               = $true
        RedirectStandardOutput = $вывод
        RedirectStandardError  = $ошибки
    }
    if ($Аргументы.Count -gt 0) {
        $параметры['ArgumentList'] = (($Аргументы | ForEach-Object { Аргумент $_ }) -join ' ')
    }
    try {
        $процесс = Start-Process @параметры
        $код = $процесс.ExitCode
    } catch {
        Запись ('ОШИБКА: не удалось запустить <{0}>: {1}' -f $Программа, $_.Exception.Message)
        return [pscustomobject]@{ Код = -1; Текст = '' }
    }
    $текст = (Прочитать_файл $вывод) + (Прочитать_файл $ошибки)
    if ($ТихийЖурнал) {
        Remove-Item -LiteralPath $вывод -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ошибки -Force -ErrorAction SilentlyContinue
    } else {
        Перенести_в_журнал $вывод
        Перенести_в_журнал $ошибки
    }
    return [pscustomobject]@{ Код = $код; Текст = $текст }
}

function Свободно_ГБ([string]$путь) {
    # Каталог может ещё не существовать (его создаст инструмент) — смотрим на диск, а не на каталог.
    try {
        $корень = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($путь))
        if (-not $корень) { return -1 }
        $диск = New-Object System.IO.DriveInfo($корень)
        if (-not $диск.IsReady) { return -1 }
        return [math]::Round($диск.AvailableFreeSpace / 1GB, 2)
    } catch {
        return -1
    }
}

# --- подготовка ---------------------------------------------------------------

if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
if (-not (Test-Path -LiteralPath $Manifest)) { Запись ("ОШИБКА: нет файла манифеста <$Manifest>"); exit 2 }
if (-not (Test-Path -LiteralPath $GitSync)) { Запись ("ОШИБКА: нет программы <$GitSync>"); exit 2 }

$Manifest = (Resolve-Path -LiteralPath $Manifest).ProviderPath
$GitSync = (Resolve-Path -LiteralPath $GitSync).ProviderPath

# Если -GitSync указывает на .cmd/.bat, аргументы заново разбирает cmd.exe (так устроен запуск
# batch-файлов в Windows). Кавычки спасают от `&` и `|`, но не от подстановки `%ПЕРЕМЕННАЯ%`.
# Тихо подставить чужое значение в путь манифеста нельзя — отказываемся до работы.
$расширение = [System.IO.Path]::GetExtension($GitSync).ToLowerInvariant()
if (@('.cmd', '.bat') -contains $расширение) {
    foreach ($значение in @($Manifest)) {
        if ($значение -match '[%"]') {
            Запись ('ОШИБКА: <{0}> — batch-файл, а аргумент <{1}> содержит % или кавычку.' -f $GitSync, $значение)
            Запись 'cmd.exe разбирает аргументы batch-файла заново и подставит вместо %ИМЯ% значение переменной окружения.'
            Запись 'Укажите -GitSync с настоящей программой (gitsync-py.exe в каталоге Scripts окружения) либо уберите % из пути.'
            exit 2
        }
    }
}

# Путь общего репозитория и каталог временных файлов берём из манифеста, если не заданы явно.
# Манифест читаем сами: BOM от «Блокнота» и Windows PowerShell 5.1 инструмент принимает
# (CLI читает файл как utf-8-sig), значит и обёртка обязана его принять.
try {
    $байты_манифеста = [System.IO.File]::ReadAllBytes($Manifest)
    $текст_манифеста = [System.Text.Encoding]::UTF8.GetString($байты_манифеста)
    if ($текст_манифеста.Length -gt 0 -and $текст_манифеста[0] -eq [char]0xFEFF) {
        $текст_манифеста = $текст_манифеста.Substring(1)
    }
    $манифест = $текст_манифеста | ConvertFrom-Json
} catch {
    Запись ('ОШИБКА: манифест не читается как JSON: ' + $_.Exception.Message)
    exit 2
}
if (-not $Repository -and $манифест.repository) { $Repository = [string]$манифест.repository }
if (-not $TempRoot -and $манифест.defaults -and $манифест.defaults.temp_root) {
    $TempRoot = [string]$манифест.defaults.temp_root
}

# --- один запуск за раз ------------------------------------------------------

$файл_блокировки = Join-Path $LogDir 'sync-and-push.lock'
try {
    $блокировка = [System.IO.File]::Open($файл_блокировки, [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch {
    Запись 'ПРОПУСК: предыдущий запуск ещё не закончился (файл блокировки занят)'
    exit 6
}

try {
    Запись '=============================================================='
    Запись ('НАЧАЛО. манифест: ' + $Manifest)
    if ($Repository) { Запись ('репозиторий: ' + $Repository) }
    if ($TempRoot) { Запись ('временные файлы: ' + $TempRoot) }

    # --- проверка свободного места ------------------------------------------
    foreach ($пара in @(@{ Имя = 'репозиторий'; Путь = $Repository }, @{ Имя = 'временные файлы'; Путь = $TempRoot })) {
        if (-not $пара.Путь) { continue }
        $свободно = Свободно_ГБ $пара.Путь
        if ($свободно -lt 0) {
            Запись ('ВНИМАНИЕ: не удалось измерить свободное место для «{0}» ({1})' -f $пара.Имя, $пара.Путь)
            continue
        }
        Запись ('свободно на диске «{0}»: {1} ГБ' -f $пара.Имя, $свободно)
        if ($свободно -lt $MinFreeGB) {
            Запись ('ОСТАНОВ: свободно {0} ГБ, требуется не менее {1} ГБ. Синхронизация не запускалась.' -f $свободно, $MinFreeGB)
            exit 3
        }
    }

    # --- синхронизация ------------------------------------------------------
    Запись 'ШАГ 1: выгрузка версий хранилищ в Git (sync-all)'
    $выгрузка = Выполнить -Программа $GitSync -Аргументы @('sync-all', '--config', $Manifest) -Шаг 1
    if ($выгрузка.Код -ne 0) {
        Запись ('ИТОГ: ЭКСПОРТ НЕ УДАЛСЯ (код {0}). Отправка в удалённый репозиторий НЕ выполнялась. Смотрите лог выше.' -f $выгрузка.Код)
        exit 4
    }
    Запись 'ШАГ 1 выполнен: новых ошибок нет'

    # --- отправка -----------------------------------------------------------
    if ($Push) {
        if (-not $Repository) {
            Запись 'ИТОГ: ЭКСПОРТ ВЫПОЛНЕН, но отправка невозможна: не задан путь репозитория (-Repository или ключ repository в манифесте)'
            exit 5
        }
        $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $git) {
            Запись 'ИТОГ: ЭКСПОРТ ВЫПОЛНЕН, отправка отменена: программа git не найдена в PATH'
            exit 5
        }
        $ветка = $Branch
        if (-not $ветка) {
            $запрос = Выполнить -Программа $git.Source -Шаг 2 -ТихийЖурнал `
                -Аргументы @('-C', $Repository, 'symbolic-ref', '--short', 'HEAD')
            if ($запрос.Код -ne 0 -or -not $запрос.Текст.Trim()) {
                Запись 'ИТОГ: ЭКСПОРТ ВЫПОЛНЕН, отправка отменена: не удалось определить текущую ветку'
                exit 5
            }
            $ветка = $запрос.Текст
        }
        $ветка = $ветка.Trim()
        Запись ('ШАГ 2: отправка ветки «{0}» в «{1}» (без принудительной перезаписи)' -f $ветка, $Remote)
        # Ссылка названа полностью, и после `--` git не примет имя за ключ: ни ветка, ни имя
        # удалённого репозитория не могут превратиться в параметр командной строки.
        $ссылка = 'refs/heads/{0}:refs/heads/{0}' -f $ветка
        $отправка = Выполнить -Программа $git.Source -Шаг 3 `
            -Аргументы @('-C', $Repository, 'push', '--', $Remote, $ссылка)
        if ($отправка.Код -ne 0) {
            if ($отправка.Текст -match 'rejected|non-fast-forward|fetch first') {
                Запись 'ИТОГ: ОТПРАВКА ОТКЛОНЕНА: в удалённом репозитории есть коммиты, которых нет локально.'
                Запись 'Ничего не исправляю сам: принудительная отправка и сброс истории запрещены. Разберите расхождение вручную.'
            } else {
                Запись ('ИТОГ: ОТПРАВКА НЕ УДАЛАСЬ (код {0}). Коммиты сохранены локально, повторный запуск отправит их снова — дублей не будет.' -f $отправка.Код)
            }
            exit 5
        }
        Запись 'ШАГ 2 выполнен: изменения в удалённом репозитории'
    } else {
        Запись 'ШАГ 2 пропущен: ключ -Push не задан, отправка в удалённый репозиторий не выполняется'
    }

    # --- что осталось от прежних запусков инструмента ------------------------
    if ($TempRoot -and (Test-Path -LiteralPath $TempRoot) -and $StaleTempDays -ge 0) {
        $граница = (Get-Date).AddDays(-1 * $StaleTempDays)
        $старые = @(Get-ChildItem -LiteralPath $TempRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { ($_.Name -like 'native-*' -or $_.Name -like 'run-*') -and $_.LastWriteTime -lt $граница })
        if ($старые.Count -gt 0) {
            Запись ('старые временные каталоги (native-*/run-*) в «{0}»: {1} шт., старше {2} дн.' -f $TempRoot, $старые.Count, $StaleTempDays)
            Запись 'обёртка каталоги не удаляет: имя каталога не доказывает ни того, что его создал инструмент, ни того, что в нём не идёт работа.'
            Запись 'Посмотрите список сами и уберите лишнее, когда синхронизация не работает (руководство по установке, раздел 13.5).'
        } else {
            Запись ('старых временных каталогов (native-*/run-*) старше {0} дн. нет; обёртка каталоги не удаляет' -f $StaleTempDays)
        }
    }
    if ($KeepLogDays -gt 0) {
        $граница = (Get-Date).AddDays(-1 * $KeepLogDays)
        $имя_текущего = [System.IO.Path]::GetFileName($log)
        $старые_логи = @(Get-ChildItem -LiteralPath $LogDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match $свои_логи -and $_.Name -ne $имя_текущего -and $_.LastWriteTime -lt $граница })
        foreach ($файл in $старые_логи) { Remove-Item -LiteralPath $файл.FullName -Force -ErrorAction SilentlyContinue }
        Запись ('уборка логов: удалено {0}, хранится {1} дн.' -f $старые_логи.Count, $KeepLogDays)
    }

    Запись 'ИТОГ: УСПЕХ'
    exit 0
} finally {
    if ($блокировка) { $блокировка.Close(); $блокировка.Dispose() }
}
