#!/bin/bash
# Штатный запуск контейнерного задания gitsync-py с ОБЩЕЙ очередью допуска.
#
# ЗАЧЕМ ОЧЕРЕДЬ. Платформа 1С удерживает лицензию на время работы конфигуратора. Когда два
# задания запускались одновременно, одному из них платформа отвечала «Не найдена лицензия»
# и оно падало с кодом 1. Очередь НИЧЕГО не меняет в лицензионном механизме, не делит
# ключ, не трогает сервер лицензий и не обходит проверку: она лишь не даёт двум заданиям
# работать одновременно. Граница честная и именно так и описана: ПОДАЧА параллельная,
# ИСПОЛНЕНИЕ последовательное. Задания, запущенные мимо этого сценария, в очереди не
# участвуют, и за них тут никто не отвечает.
#
# ЧТО ЭТО НЕ ДЕЛАЕТ. Это не доказательство одноместности лицензии и не попытка её обойти.
# Если ваша лицензия допускает несколько одновременных сеансов — увеличьте GITSYNC_SLOTS
# ровно настолько, насколько это ПОДТВЕРЖДЕНО, и не больше.
#
# ДОГОВОР О КАТАЛОГЕ ЗАПУСКА (stage-18). Задание выгружает версии в каталог запуска
# (scratch) — это ДАННЫЕ задания. Всё ниже замерено на стенде двумя штатными
# контейнерами с одним и тем же UID 10001:
#
#   каталог запуска на bind-mount хоста  — сосед видит, читает и уничтожает `rm -rf`
#   каталог запуска на томе docker       — то же (и именованный, и анонимный)
#   каталог запуска на слое контейнера   — сосед не видит вовсе, выгрузка цела
#   каталог запуска на tmpfs контейнера  — то же, сосед не видит
#
# Поэтому общий том под каталогом запуска НЕ ПОДДЕРЖИВАЕТСЯ: сценарий отказывается
# запускать задание, если монтирование накрывает каталог запуска. Отказ происходит ДО
# старта контейнера, то есть до любого удаления. Это не «принимается с оговоркой».
#
# Из двух частных вариантов выбран tmpfs, и тоже по замеру, а не по вкусу: при
# параллельной выгрузке (jobs=2) конфигуратор на overlay отвечает «Не найдена лицензия»
# и доводит 13-14 версий из 17, а на tmpfs даёт 17 из 17. Подробности — у объявления
# SCRATCH_MOUNT ниже.
#
# ЧТО ЭТИМ НЕ ЗАКРЫТО. Враждебный код, работающий под тем же UID ВНУТРИ этого же
# контейнера, по-прежнему вне границы: в POSIX удаление адресует имя, и сохранность
# чужого объекта при том же UID недостижима (измерения stage-17). Частный scratch
# закрывает соседний КОНТЕЙНЕР, а не соседний процесс внутри своего.
#
# Использование:
#   GITSYNC_IMAGE=sha256:<id> examples/gitsync-run.sh <аргументы gitsync-py>
#   examples/gitsync-run.sh --help
# Монтирования и прочие ключи docker передаются переменной GITSYNC_DOCKER_ARGS, а не
# вперемешку с аргументами задания: иначе не разобрать, где кончается одно и начинается
# другое.
#
# Переменные окружения — см. examples/gitsync-run.env.example.
set -u

PROGRAM=${0##*/}
EX_USAGE=64
EX_BUSY=75          # EX_TEMPFAIL: очередь занята, попробуйте позже — это НЕ отказ задания
EX_UNSAFE=77        # EX_NOPERM: каталог очереди небезопасен
EX_CONTRACT=78      # EX_CONFIG: монтирования нарушают договор о каталоге запуска

usage() {
    cat <<'TEXT'
gitsync-run.sh — один контейнерный запуск gitsync-py под общей очередью допуска.

  GITSYNC_IMAGE=<id|тег>      обязательно: образ с платформой (закрепляйте по sha256)
  GITSYNC_ADMISSION_DIR=<путь> каталог очереди (умолчание /var/tmp/gitsync-admission)
  GITSYNC_ADMISSION_WAIT=<с>  предел ожидания в очереди (умолчание 1800)
  GITSYNC_SLOTS=<N>           число одновременных заданий (умолчание 1)
  GITSYNC_DOCKER_ARGS=<...>   дополнительные аргументы docker run (монтирования, сеть)
  GITSYNC_NAME_PREFIX=<имя>   префикс имени контейнера (умолчание gitsync-job)
  GITSYNC_SCRATCH=<путь>      каталог запуска внутри контейнера (умолчание
                              /var/lib/gitsync/scratch). Монтировать его, его предков
                              или что-либо внутри него нельзя — сценарий откажет кодом 78.
  GITSYNC_SCRATCH_TMPFS=<N>   размер tmpfs под каталог запуска (умолчание 2g).
                              `off` — не поднимать tmpfs; тогда каталог запуска
                              останется на слое контейнера, где параллельная выгрузка
                              (jobs>1) НЕ проверена (замер stage-18).

Монтируйте рабочую копию ЛИСТОМ (-v /хост/repo:/work/repo), а не целиком /work: иначе
каталог запуска попадёт на общий с хостом том и будет доступен другому контейнеру.

Коды возврата: код контейнера как есть; 64 — ошибка вызова; 75 — очередь занята дольше
предела ожидания; 77 — каталог очереди небезопасен; 78 — монтирования нарушают договор о
каталоге запуска; 130/143 — прерывание сигналом.
TEXT
}

if [ "${1-}" = "--help" ] || [ "${1-}" = "-h" ]; then usage; exit 0; fi

IMAGE=${GITSYNC_IMAGE-}
if [ -z "$IMAGE" ]; then
    echo "$PROGRAM: не задан GITSYNC_IMAGE" >&2
    usage >&2
    exit $EX_USAGE
fi
if [ "$#" -eq 0 ]; then
    echo "$PROGRAM: не заданы аргументы задания" >&2
    exit $EX_USAGE
fi
command -v docker >/dev/null 2>&1 || { echo "$PROGRAM: docker не найден" >&2; exit $EX_USAGE; }
command -v flock >/dev/null 2>&1 || { echo "$PROGRAM: flock не найден" >&2; exit $EX_USAGE; }

ADMISSION_DIR=${GITSYNC_ADMISSION_DIR:-/var/tmp/gitsync-admission}
WAIT=${GITSYNC_ADMISSION_WAIT:-1800}
SLOTS=${GITSYNC_SLOTS:-1}
PREFIX=${GITSYNC_NAME_PREFIX:-gitsync-job}
case "$WAIT" in ''|*[!0-9]*) echo "$PROGRAM: GITSYNC_ADMISSION_WAIT — целое" >&2; exit $EX_USAGE;; esac
case "$SLOTS" in ''|*[!0-9]*) echo "$PROGRAM: GITSYNC_SLOTS — целое" >&2; exit $EX_USAGE;; esac
[ "$SLOTS" -ge 1 ] || { echo "$PROGRAM: GITSYNC_SLOTS не меньше 1" >&2; exit $EX_USAGE; }

# --- каталог очереди: один владелец, никакой групповой и общей записи --------------
# Замок нужен, чтобы координировать СВОИ задания. Каталог, в который может писать кто-то
# ещё, этого не обеспечивает: чужой процесс подменит файл замка, и очередь развалится.
# Поэтому каталог либо создаём сами (umask 077), либо проверяем и отказываемся работать.
( umask 077 && mkdir -p "$ADMISSION_DIR" ) || exit $EX_UNSAFE
owner=$(stat -c '%u' "$ADMISSION_DIR" 2>/dev/null || echo "?")
mode=$(stat -c '%a' "$ADMISSION_DIR" 2>/dev/null || echo "?")
if [ "$owner" != "$(id -u)" ]; then
    echo "$PROGRAM: каталог очереди $ADMISSION_DIR принадлежит uid $owner, а не $(id -u): " \
         "очередь общая только для заданий ОДНОГО пользователя" >&2
    exit $EX_UNSAFE
fi
case "$mode" in
    7[0-7][0-7]) ;;
    *) echo "$PROGRAM: подозрительные права $mode на $ADMISSION_DIR" >&2; exit $EX_UNSAFE;;
esac
if [ "$(( 0$mode & 022 ))" -ne 0 ]; then
    echo "$PROGRAM: в $ADMISSION_DIR может писать не только владелец (права $mode): " \
         "замок в таком каталоге ничего не гарантирует" >&2
    exit $EX_UNSAFE
fi

# --- договор о каталоге запуска: проверяется ДО старта контейнера --------------------
# Разбираются ровно те ключи docker, которые создают точку монтирования, и аргумент
# задания --temp-root. Если каталог запуска пересекается хотя бы с одним монтированием —
# отказ. Молча «исправлять» чужую команду нельзя: оператор обязан узнать, что его режим
# не поддерживается, а не получить втихую другой каталог.
# Умолчание совпадает с каталогом, который образ создаёт на СВОЁМ слое (Dockerfile.core).
# Значение передаётся в контейнер ниже, поэтому проверенное здесь и применённое там —
# всегда одно и то же: разойтись они не могут.
SCRATCH_DEFAULT=${GITSYNC_SCRATCH:-/var/lib/gitsync/scratch}
MOUNT_TARGETS=()

norm_path() { # абсолютный путь без хвостовых и сдвоенных слэшей
    local p=$1
    case "$p" in /*) ;; *) return 1;; esac
    while case "$p" in *//*) true;; *) false;; esac; do p=${p//\/\//\/}; done
    while [ "$p" != "/" ] && [ "${p%/}" != "$p" ]; do p=${p%/}; done
    printf '%s' "$p"
}

within() { # within A B — истина, когда A совпадает с B или лежит внутри B
    [ "$1" = "$2" ] && return 0
    [ "$2" = "/" ] && return 0
    case "$1" in "$2"/*) return 0;; esac
    return 1
}

remember_target() {
    local target
    target=$(norm_path "$1") || return 0   # относительный путь монтированием не является
    MOUNT_TARGETS+=("$target")
}

# -v SRC:DST[:опции] либо -v DST (анонимный том)
add_volume_spec() {
    local spec=$1 second
    case "$spec" in
        *:*) second=${spec#*:}; remember_target "${second%%:*}";;
        *)   remember_target "$spec";;
    esac
}
# --mount type=...,dst=DST (он же destination/target)
add_mount_spec() {
    local field
    local IFS=,
    for field in $1; do
        case "$field" in
            dst=*|destination=*|target=*) remember_target "${field#*=}";;
        esac
    done
}
# --tmpfs DST[:опции]
add_tmpfs_spec() { remember_target "${1%%:*}"; }

collect_mount_targets() {
    local words word next index count
    # shellcheck disable=SC2206 — GITSYNC_DOCKER_ARGS намеренно разбивается на слова
    words=( ${GITSYNC_DOCKER_ARGS-} )
    count=${#words[@]}
    index=0
    while [ "$index" -lt "$count" ]; do
        word=${words[$index]}
        next=${words[$((index + 1))]-}
        case "$word" in
            -v|--volume)  index=$((index + 1)); add_volume_spec "$next";;
            --volume=*)   add_volume_spec "${word#--volume=}";;
            -v?*)         add_volume_spec "${word#-v}";;
            --mount)      index=$((index + 1)); add_mount_spec "$next";;
            --mount=*)    add_mount_spec "${word#--mount=}";;
            --tmpfs)      index=$((index + 1)); add_tmpfs_spec "$next";;
            --tmpfs=*)    add_tmpfs_spec "${word#--tmpfs=}";;
        esac
        index=$((index + 1))
    done
}

# Каталог запуска, который получит задание: --temp-root, если он задан, иначе умолчание.
effective_scratch() {
    local argument previous= found=
    for argument in "$@"; do
        case "$argument" in
            --temp-root=*) found=${argument#--temp-root=};;
            *) [ "$previous" = "--temp-root" ] && found=$argument;;
        esac
        previous=$argument
    done
    printf '%s' "${found:-$SCRATCH_DEFAULT}"
}

collect_mount_targets
SCRATCH=$(effective_scratch "$@")
if ! SCRATCH=$(norm_path "$SCRATCH"); then
    echo "$PROGRAM: --temp-root обязан быть абсолютным путём внутри контейнера" >&2
    exit $EX_CONTRACT
fi
for target in ${MOUNT_TARGETS[@]+"${MOUNT_TARGETS[@]}"}; do
    if within "$SCRATCH" "$target" || within "$target" "$SCRATCH"; then
        echo "$PROGRAM: монтирование $target накрывает каталог запуска $SCRATCH." >&2
        echo "  Каталог запуска обязан оставаться на собственном слое контейнера: на общем" >&2
        echo "  томе его видит и уничтожает другой контейнер с тем же UID (замер stage-18)." >&2
        echo "  Монтируйте рабочую копию листом, например -v /хост/repo:/work/repo." >&2
        exit $EX_CONTRACT
    fi
done

# --- чем именно обеспечивается частный каталог запуска -------------------------------
# Частных вариантов два, и оба ЗАМЕРЕНЫ как недостижимые для соседнего контейнера:
# собственный слой контейнера (overlay) и tmpfs, поднятая в его пространстве монтирования.
# По изоляции они равны. По РАБОТОСПОСОБНОСТИ — нет, и это тоже замерено:
#
#   каталог запуска на overlay      jobs=2 -> «Не найдена лицензия», 14 и 13 коммитов из 17
#   каталог запуска на tmpfs        jobs=2 -> 17 из 17, код 0
#   каталог запуска на томе хоста   jobs=2 -> 17 из 17, код 0 (но изоляции нет)
#
# То есть конфигуратор при параллельной выгрузке не уживается с overlay. Причина не
# установлена и здесь не выдумывается; установлено ГДЕ он работает. Поэтому каталог
# запуска поднимается как tmpfs — частный И рабочий.
#
# mode=1777 вместо угадывания uid/gid: контейнер запускается под UID оператора
# (`-u` в GITSYNC_DOCKER_ARGS), и сценарий этого UID не знает. Внутри частного
# контейнера «все» — это процессы самого контейнера, то есть ровно та область, которая
# и так объявлена доверенной. Липкий бит от соседа с ТЕМ ЖЕ UID не спасает и здесь этого
# не обещает — граница прежняя.
#
# Память: tmpfs живёт в ОЗУ. Размер задаётся GITSYNC_SCRATCH_TMPFS (умолчание 2g).
# Значение `off` отключает tmpfs — тогда каталог запуска остаётся на слое контейнера,
# и параллельная выгрузка (jobs>1) на нём НЕ проверена.
TMPFS_SIZE=${GITSYNC_SCRATCH_TMPFS:-2g}
if [ "$TMPFS_SIZE" = "off" ]; then
    SCRATCH_MOUNT=
    # log() определяется ниже — здесь пишем напрямую.
    echo "$PROGRAM: tmpfs отключена, каталог запуска остаётся на слое контейнера;" \
         "параллельная выгрузка (jobs>1) на нём НЕ проверена" >&2
else
    SCRATCH_MOUNT="--tmpfs $SCRATCH:rw,mode=1777,size=$TMPFS_SIZE"
fi

JOB_ID=$$-$(date +%s)
NAME="$PREFIX-$JOB_ID"
# Метка уникальна для ЭТОГО запуска: по ней и только по ней находится свой контейнер.
# Имя для остановки не годится — под ним может оказаться чужой контейнер.
JOB_TAG=$(cat /proc/sys/kernel/random/uuid 2>/dev/null \
          || od -An -tx1 -N16 /dev/urandom | tr -d ' \n')
CONTAINER_PID=
STARTED=

log() { printf '%s %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PROGRAM" "$*" >&2; }

own_containers() {
    # Свои контейнеры ищутся ПО СВОЕЙ МЕТКЕ, а не по имени. Имя предсказуемо и может
    # принадлежать чужому контейнеру (совпадение префикса, перезапуск, ручной запуск);
    # остановка по имени — остановка чужой работы. Метка — свежий UUID этого запуска,
    # и её нет ни у кого другого. Пусто — значит нашего контейнера ещё/уже нет, и
    # останавливать НЕЧЕГО: ни одной команды docker в этом случае не выполняется.
    docker ps -aq --no-trunc --filter "label=com.gitsync.job=$JOB_TAG" 2>/dev/null || true
}

stop_container() {
    local id
    for id in $(own_containers); do
        docker stop -t 30 "$id" >/dev/null 2>&1 || true
    done
}

on_signal() {
    signal=$1
    log "получен SIG$signal, останавливаю свои контейнеры (метка $JOB_TAG)"
    stop_container
    if [ -n "$CONTAINER_PID" ]; then wait "$CONTAINER_PID" 2>/dev/null; fi
    for id in $(own_containers); do
        docker rm -f "$id" >/dev/null 2>&1 || true
    done
    case "$signal" in
        INT) exit 130;;
        *) exit 143;;
    esac
}

# --- очередь -----------------------------------------------------------------------
# Слот — отдельный файл замка. Файлы НЕ удаляются никогда: удаление файла, на котором
# висит чужой flock, снимает координацию (новый процесс создаст файл заново и получит
# замок, пока прежний ещё работает). Ожидание ограничено: занятая очередь — понятный
# код 75, а не бесконечное молчание.
wait_started=$(date +%s)
slot=
for index in $(seq 1 "$SLOTS"); do
    lock="$ADMISSION_DIR/slot-$index.lock"
    ( umask 077 && : >> "$lock" ) || exit $EX_UNSAFE
    exec 9>>"$lock" || exit $EX_UNSAFE
    if flock -n 9; then slot=$index; break; fi
    exec 9>&-
done
if [ -z "$slot" ]; then
    log "все слоты ($SLOTS) заняты, жду освобождения не дольше ${WAIT}s"
    lock="$ADMISSION_DIR/slot-1.lock"
    exec 9>>"$lock" || exit $EX_UNSAFE
    if ! flock -w "$WAIT" 9; then
        log "очередь занята дольше ${WAIT}s: задание не запускалось"
        exit $EX_BUSY
    fi
    slot=1
fi
waited=$(( $(date +%s) - wait_started ))
log "допуск получен: слот $slot, ожидание ${waited}s, контейнер $NAME"

trap 'on_signal INT' INT
trap 'on_signal TERM' TERM

STARTED=$(date +%s)
# shellcheck disable=SC2086 — GITSYNC_DOCKER_ARGS намеренно разбивается на слова
# GITSYNC_SCRATCH и требование частного каталога ставятся ПОСЛЕ аргументов оператора:
# проверенный здесь путь обязан быть тем же, что применит задание. Собственную tmpfs
# оператор перекрыть не может: монтирование в каталог запуска уже отвергнуто выше.
# shellcheck disable=SC2086 — GITSYNC_DOCKER_ARGS намеренно разбивается на слова
docker run --rm --name "$NAME" --label "com.gitsync.job=$JOB_TAG" \
    ${GITSYNC_DOCKER_ARGS-} ${SCRATCH_MOUNT-} \
    -e "GITSYNC_SCRATCH=$SCRATCH" -e GITSYNC_REQUIRE_PRIVATE_SCRATCH=1 \
    "$IMAGE" "$@" &
CONTAINER_PID=$!
wait "$CONTAINER_PID"
rc=$?
FINISHED=$(date +%s)
trap - INT TERM
# Файл замка остаётся на месте; освобождает его закрытие дескриптора при выходе.
log "контейнер $NAME завершился с кодом $rc (слот $slot, работа $((FINISHED - STARTED))s)"
exit "$rc"
