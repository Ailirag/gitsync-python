"""Безопасная сборка путей внутри каталога и удаление СВОИХ каталогов.

Выгрузка и фикстуры — внешние данные: имя файла может содержать ``..`` или быть абсолютным.
Любая запись за пределы своего каталога считается ошибкой, а не «само пройдёт».

Удаление одноразового состояния — та же граница, только наружу: путь, по которому каталог
создавали, к моменту очистки может вести куда угодно (ссылка, соединение NTFS, просто другой
каталог на том же месте). Поэтому удаляется не «путь», а ЗАКРЕПЛЁННЫЙ объект: каталог
сначала открывается так, что подменить его уже нельзя, удостоверение сверяется у открытого
объекта, и только потом снимается содержимое. Закрепление в POSIX — дескриптор каталога
(``O_DIRECTORY | O_NOFOLLOW`` и операции с ``dir_fd``), на Windows — описатель без
``FILE_SHARE_DELETE`` (:mod:`gitsync.winfs`). Модель угроз и оставшееся окно —
docs/plugins.md.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
from pathlib import Path, PurePath

from . import winfs
from .errors import UnsafePathError

log = logging.getLogger("gitsync.safepath")

_WINDOWS = os.name == "nt"

#: Содержимое своего каталога снимается через ЗАКРЕПЛЁННЫЙ дескриптор: POSIX умеет открыть
#: каталог и удалять записи относительно него (``dir_fd``), и подмена ПУТИ после проверки
#: тогда уже никуда не уводит — дескриптор держит сам объект.
_PINNED_REMOVAL = (not _WINDOWS and hasattr(os, "O_DIRECTORY")
                   and shutil.rmtree.avoids_symlink_attacks
                   and os.rmdir in os.supports_dir_fd)

#: На Windows роль дескриптора играет описатель каталога, открытый без ``FILE_SHARE_DELETE``:
#: пока он открыт, ни сам каталог, ни его предки не переименовываются, а снимается каталог
#: по описателю, а не по пути (подробности и измерения — :mod:`gitsync.winfs`).
_WINDOWS_PINNED = _WINDOWS and winfs.AVAILABLE

#: Удостоверение каталога: пары «устройство, inode» достаточно, чтобы отличить СВОЙ каталог
#: от чужого, оказавшегося на том же пути. Имя и путь удостоверением не являются.
Identity = tuple[int, int]


def _identity_of(info: os.stat_result) -> Identity | None:
    """Удостоверение каталога по снятой БЕЗ перехода по ссылке ``stat``.

    Ссылка и точка повторного разбора NTFS (junction) каталогом здесь не считаются: их
    собственное удостоверение принадлежит ссылке, а рекурсивное удаление по ним уходит в
    чужое дерево. ``None`` означает «своим быть не может».

    Нулевой индекс — не удостоверение: файловая система, не отдающая ``st_ino``, сделала бы
    разные каталоги неразличимыми, и сравнение «это тот же объект» потеряло бы смысл.
    Такой том обслуживается отказом от очистки, а не удалением наугад.
    """
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if getattr(info, "st_file_attributes", 0) & reparse or getattr(info, "st_reparse_tag", 0):
        return None
    if not info.st_ino:
        return None
    return (info.st_dev, info.st_ino)


def directory_identity(path: str | Path) -> Identity | None:
    """Удостоверение каталога по пути, без перехода по ссылке на последнем звене."""
    try:
        return _identity_of(os.lstat(path))
    except OSError:
        return None


#: Контейнерная поставка требует, чтобы каталог запуска был ЧАСТНЫМ (stage-18).
#: Переменная выставляется пусковым сценарием и образом; вне контейнера её нет, и
#: поведение обычной установки не меняется ни на байт.
PRIVATE_SCRATCH_ENV = "GITSYNC_REQUIRE_PRIVATE_SCRATCH"

#: Каталог запуска, объявленный поставкой. В образе это путь на СОБСТВЕННОМ слое
#: контейнера; вне контейнера переменной нет, и умолчание остаётся прежним — рядом с
#: рабочей копией. Явно заданный оператором ``temp_root`` сильнее обоих: он не
#: игнорируется, а проверяется и при общем томе отвергается.
SCRATCH_DIR_ENV = "GITSYNC_SCRATCH"

_MOUNTINFO = "/proc/self/mountinfo"


def default_scratch_root(fallback: Path) -> Path:
    """Каталог запуска по умолчанию: объявленный поставкой либо ``fallback``."""
    declared = os.environ.get(SCRATCH_DIR_ENV, "").strip()
    return Path(declared) if declared else fallback


def _mount_backing(path: Path) -> dict[str, str] | None:
    """Точка монтирования, на которой окажется ``path``.

    Каталог запуска на момент проверки ещё НЕ СОЗДАН — в этом и смысл: отказ обязан
    случиться до создания. Поэтому точка монтирования определяется по самому пути, а не
    по ближайшему существующему предку: если ``/scratch`` смонтирован, то ``/scratch/run-1``
    лежит на нём независимо от того, создан он уже или нет. Подъём к существующему предку
    давал бы для несозданного каталога ответ про ДРУГУЮ файловую систему — и проверка
    пропускала бы ровно тот случай, ради которого написана.

    Ссылки в существующей части пути разыменовываются (``realpath``): иначе точку
    монтирования можно было бы подменить символической ссылкой.

    ``None`` — сведений о монтированиях нет (не Linux либо ``/proc`` не смонтирован).
    """
    try:
        with open(_MOUNTINFO, encoding="utf-8") as handle:
            records = []
            for line in handle:
                fields = line.split()
                # ... 3=корень внутри источника, 4=точка монтирования, затем «-», тип, источник
                if "-" not in fields:
                    continue
                separator = fields.index("-")
                records.append({"root": fields[3], "mount_point": fields[4],
                                "fstype": fields[separator + 1], "source": fields[separator + 2]})
    except OSError:
        return None
    if not records:
        return None
    target = os.path.realpath(str(path))
    covering = [record for record in records
                if target == record["mount_point"]
                or target.startswith(record["mount_point"].rstrip("/") + "/")]
    if not covering:
        return None
    # Побеждает самая длинная точка монтирования: она и есть ближайшая.
    return max(covering, key=lambda record: len(record["mount_point"]))


def scratch_is_private(path: str | Path) -> tuple[bool, dict[str, str] | None]:
    """Лежит ли каталог запуска на файловой системе, ЧАСТНОЙ для этого контейнера.

    Частным считается ровно то, что измерено как недостижимое для соседнего контейнера
    (evidence/stage-18): собственный слой контейнера (точка монтирования ``/``) и
    ``tmpfs``, поднятая внутри этого пространства монтирования. Всё остальное —
    отдельная точка монтирования: bind-mount каталога хоста или том docker. И то и
    другое замерено как ДОСТИЖИМОЕ: другой штатный контейнер с тем же UID видит
    выгрузку, читает её и уничтожает ``rm -rf`` (10-isolation-probe.log — bind,
    13-volume-probe.log — именованный и анонимный том).

    Честная граница метода: он читает ``mountinfo``, то есть отвечает на вопрос «своё
    ли это пространство монтирования», а не «кто ещё имеет доступ». Контейнер, которому
    корнем отдали каталог хоста, отличить нельзя — такой режим поставкой не
    предусмотрен и здесь не проверяется.
    """
    backing = _mount_backing(Path(path))
    if backing is None:
        return False, None
    private = backing["mount_point"] == "/" and backing["root"] == "/"
    private = private or (backing["fstype"] == "tmpfs" and backing["root"] == "/")
    return private, backing


def require_private_scratch(path: str | Path, what: str = "Каталог запуска") -> None:
    """Отказ ДО создания и ДО любого удаления, если каталог запуска не частный.

    Проверка включается переменной :data:`PRIVATE_SCRATCH_ENV` — её выставляет
    контейнерная поставка. Без неё (Windows, обычная установка) функция не делает
    ничего: там общий каталог временных файлов — осознанный режим оператора.
    """
    if os.environ.get(PRIVATE_SCRATCH_ENV, "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    private, backing = scratch_is_private(path)
    if private:
        return
    if backing is None:
        raise UnsafePathError(
            f"{what} <{path}>: сведения о монтированиях недоступны, а поставка требует "
            f"частного каталога запуска ({PRIVATE_SCRATCH_ENV}). Отказ до создания каталога."
        )
    raise UnsafePathError(
        f"{what} <{path}> лежит на общем томе: точка монтирования "
        f"<{backing['mount_point']}> типа <{backing['fstype']}> из <{backing['source']}>. "
        "Такой каталог виден другому контейнеру с тем же UID — он читает и уничтожает "
        "выгрузку (замер stage-18). Укажите каталог на собственном слое контейнера "
        "(например /var/lib/gitsync/scratch) либо поднимите tmpfs. "
        "Ничего не создано и не удалено."
    )


#: Флаг ядра: переименовать, ТОЛЬКО если цели ещё нет. Обычный ``rename`` в POSIX
#: молча ЗАМЕЩАЕТ пустой каталог-цель — именно этим прежняя версия уничтожала чужой
#: каталог, появившийся на исходном имени (stage-15, EVA-13).
_RENAME_NOREPLACE = 1
_RENAMEAT2_SYSCALL = {"x86_64": 316, "aarch64": 276, "i386": 353, "i686": 353}

_renameat2_state: list = []


def _renameat2():
    """``renameat2`` из libc или через ``syscall``; ``None``, если примитива нет.

    Проверка «а нет ли уже такого имени» с последующим ``rename`` доказательством
    безопасности НЕ является: между проверкой и переименованием цель может появиться.
    Нужна одна неделимая операция, и она в ядре есть.
    """
    if _renameat2_state:
        return _renameat2_state[0]
    call = None
    if not _WINDOWS:
        try:
            import ctypes
            import ctypes.util
            import platform

            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            if hasattr(libc, "renameat2"):
                native = libc.renameat2
                native.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                   ctypes.c_char_p, ctypes.c_uint]

                def call(old_fd, old, new_fd, new, flags, _fn=native, _ct=ctypes):
                    _ct.set_errno(0)
                    rc = _fn(old_fd, os.fsencode(old), new_fd, os.fsencode(new), flags)
                    return rc, _ct.get_errno()
            else:
                number = _RENAMEAT2_SYSCALL.get(platform.machine())
                if number is not None:
                    native = libc.syscall
                    native.restype = ctypes.c_long

                    def call(old_fd, old, new_fd, new, flags, _fn=native, _ct=ctypes,
                             _nr=number):
                        _ct.set_errno(0)
                        rc = _fn(_ct.c_long(_nr), _ct.c_int(old_fd),
                                 _ct.c_char_p(os.fsencode(old)), _ct.c_int(new_fd),
                                 _ct.c_char_p(os.fsencode(new)), _ct.c_uint(flags))
                        return rc, _ct.get_errno()
        except Exception:  # noqa: BLE001 — нет ctypes/libc: примитива просто нет
            call = None
    _renameat2_state.append(call)
    return call


def rename_noreplace(old: str, new: str, dir_fd: int) -> None:
    """Переименование БЕЗ замены цели. Цель занята — :class:`FileExistsError`.

    ``NotImplementedError`` означает, что ядро или файловая система такого не умеют;
    вызывающий обязан выбрать безопасный отказ, а не «ну тогда обычным rename».
    """
    call = _renameat2()
    if call is None:
        raise NotImplementedError("renameat2(RENAME_NOREPLACE) недоступен")
    rc, err = call(dir_fd, old, dir_fd, new, _RENAME_NOREPLACE)
    if rc == 0:
        return
    if err == errno.EEXIST:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), new)
    if err in (errno.ENOSYS, errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP):
        # Ядро без renameat2 или файловая система без поддержки флага.
        raise NotImplementedError(f"renameat2(RENAME_NOREPLACE) не поддержан: errno={err}")
    raise OSError(err, os.strerror(err), old)


def _empty_pinned_directory(fd: int, protect: frozenset[str] = frozenset()) -> None:
    """Снимает содержимое каталога относительно уже проверенного дескриптора.

    Всё удаляется ОТНОСИТЕЛЬНО дескриптора: подмена пути после проверки удаление наружу не
    уводит. Вложенная ссылка снимается как запись каталога — переход по ней невозможен,
    потому что каталогом она не считается.

    ``protect`` — имена верхнего уровня, которые трогать НЕЛЬЗЯ (stage-14, S4b). Это те
    записи, снятие которых уже было отклонено по удостоверению: раз мы признали, что
    объект под этим именем не наш, рекурсивная уборка родителя тем более не имеет права
    его уничтожать. Оставленная запись делает каталог непустым, и снятие самого каталога
    честно не состоится.
    """
    for name in os.listdir(fd):
        if name in protect:
            log.warning("Запись не снимается: её принадлежность уже отклонена: %s", name)
            continue
        if stat.S_ISDIR(os.lstat(name, dir_fd=fd).st_mode):
            shutil.rmtree(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


def _discard_pinned_posix(path: Path, identity: Identity, what: str,
                          protect: frozenset[str] = frozenset(),
                          displaced: set[str] | None = None) -> bool:
    """POSIX: содержимое — через закреплённый дескриптор, сам каталог — через родителя."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if _identity_of(os.fstat(fd)) != identity:
            log.warning("%s перестал быть своим, очистка отменена: %s", what, path)
            return False
        _empty_pinned_directory(fd, protect)
        return _rmdir_emptied_posix(path, identity, fd, what, displaced)
    finally:
        os.close(fd)


def _rmdir_emptied_posix(path: Path, identity: Identity, pinned_fd: int, what: str,
                         displaced: set[str] | None = None) -> bool:
    """Снимает опустевший каталог через дескриптор РОДИТЕЛЯ, сверив две вещи перед удалением.

    ``rmdir`` по полному пути разбирал бы каждое звено заново, и подменённый предок увёл бы
    удаление в чужой каталог. Здесь родитель открывается один раз и сверяется ДВАЖДЫ:

    1. родитель, открытый по пути, обязан быть настоящим родителем ЗАКРЕПЛЁННОГО каталога
       (``..`` относительно его дескриптора) — иначе путь уже ведёт не к нам, и снимать по
       нему нечего: отказ, каталог остаётся на учёте;
    2. запись с нашим именем в этом родителе обязана иметь наше удостоверение.

    Раньше вторая проверка отсутствовала, а ``FileNotFoundError`` на подменённом пути
    считался успехом: свой опустошённый каталог молча оставался на диске.

    **Снятие идёт не по имени, а по ИЗОЛИРОВАННОМУ объекту** (stage-14, S6). Сверка «это
    наша запись» и сам ``rmdir`` — две разные операции, и между ними запись можно
    подменить: раньше в это окно попадал ЧУЖОЙ пустой каталог, и снимался он. Теперь
    запись сначала переименовывается в имя, которого никто снаружи не знает, и
    удостоверение сверяется у ТОГО, ЧТО ПЕРЕЕХАЛО. Чужой объект, попавший под наше имя,
    обнаруживается уже после изоляции, возвращается на место и не удаляется.

    Остаётся честная граница: если подмена случилась ДО изоляции, чужой каталог будет
    кратко переименован и возвращён. Это видимое в пространстве имён действие, но не
    разрушительное — содержимое и ссылки не затрагиваются. Полной неуязвимости к
    одновременному тому же пользователю в POSIX нет: удаления каталога по дескриптору тут
    не существует. На Windows этого окна нет — там каталог снимается по описателю
    (:mod:`gitsync.winfs`).

    Отсюда вторая честная граница (stage-16, EVA-14). Сам ``rmdir`` всё равно адресует
    объект ПО ИМЕНИ, поэтому уничтожение чужого ПУСТОГО каталога, подставленного на имя
    изоляции в окно между сверкой и снятием, устранить нельзя — ``rmdir`` по дескриптору в
    POSIX отсутствует. Устранимо и устранено другое: выдавать такой исход за успешную
    уборку. После снятия проверяется число ссылок закреплённого дескриптора, и при живом
    своём каталоге возвращается отказ, а не ``True``.
    """
    if not path.name:
        log.warning("%s не имеет имени в родителе, очистка отменена: %s", what, path)
        return False
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        real = os.stat("..", dir_fd=pinned_fd)
        opened = os.fstat(parent)
        if (real.st_dev, real.st_ino) != (opened.st_dev, opened.st_ino):
            log.warning("%s опустошён, но путь к нему ведёт уже в другой каталог, "
                        "снятие отменено: %s", what, path)
            return False
        try:
            entry = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            # stage-14, EVA-2b: пропавшее ИМЯ — ещё не снятый КАТАЛОГ. Если наш каталог
            # просто переименовали, он жив, и объявлять очистку состоявшейся нельзя.
            # Открытый дескриптор отвечает на это точно: у снятого каталога число ссылок
            # падает до нуля, у переименованного остаётся прежним.
            if os.fstat(pinned_fd).st_nlink > 0:
                log.warning("%s: записи с нашим именем нет, но сам каталог ещё существует "
                            "(переименован); очистка НЕ состоялась: %s", what, path)
                return False
            return True  # записи нет и каталога нет: цель достигнута
        if _identity_of(entry) != identity:
            log.warning("%s опустошён, но запись с этим именем принадлежит уже не нам "
                        "и оставлена на месте: %s", what, path)
            return False
        # Изоляция. Имя выбирается случайно, но на его «неизвестность снаружи»
        # полагаться НЕЛЬЗЯ: каталог-родитель читаем, и имя видно любому, кто смотрит.
        # Поэтому и занятие имени, и возврат делаются ТОЛЬКО неделимой операцией без
        # замены: занятая цель — отказ, а не молчаливое уничтожение того, что там есть.
        isolated = ""
        try:
            for _ in range(8):
                candidate = f".gitsync-discard-{os.urandom(8).hex()}"
                try:
                    rename_noreplace(path.name, candidate, parent)
                except FileExistsError:
                    continue  # имя занято — берём другое, ничего не трогая
                isolated = candidate
                break
        except NotImplementedError as exc:
            # Без неделимого переименования безопасно снять каталог нечем: обычный
            # ``rmdir`` по имени снял бы чужой пустой каталог, подставленный в окно.
            log.warning("%s: нет неделимого переименования без замены (%s); снятие "
                        "отменено, каталог оставлен: %s", what, exc, path)
            return False
        except FileNotFoundError:
            # Имя исчезло между сверкой и изоляцией. Снят каталог или просто
            # переименован — отвечает тот же признак, что и выше: число ссылок.
            if os.fstat(pinned_fd).st_nlink > 0:
                log.warning("%s: имя исчезло до изоляции, но сам каталог ещё существует "
                            "(переименован); очистка НЕ состоялась: %s", what, path)
                return False
            return True  # каталог действительно снят кем-то ещё: цель достигнута
        except OSError as exc:
            log.warning("%s: изолировать перед снятием не удалось, каталог оставлен: %s (%s)",
                        what, path, exc)
            return False
        if not isolated:
            log.warning("%s: свободное имя для изоляции не нашлось, каталог оставлен: %s",
                        what, path)
            return False

        def put_back(subject: str) -> bool:
            """Возврат на исходное имя. Занятая цель НЕ замещается (stage-15, EVA-13)."""
            try:
                rename_noreplace(isolated, path.name, parent)
                return True
            except FileExistsError:
                # На исходном имени уже что-то есть. Замещать нельзя: это уничтожило бы
                # объект, который мы только что признали чужим. Оставляем как есть и
                # честно сообщаем, ГДЕ теперь лежит перемещённое.
                if displaced is not None:
                    displaced.add(isolated)
                log.warning("%s: вернуть %s на исходное имя нельзя — оно занято другим "
                            "объектом, замещать его недопустимо. Перемещённое лежит в "
                            "%s под именем %s", what, subject, path.parent, isolated)
                return False
            except (OSError, NotImplementedError) as exc:
                if displaced is not None:
                    displaced.add(isolated)
                log.error("%s: вернуть %s на исходное имя не удалось (%s). Перемещённое "
                          "лежит в %s под именем %s", what, subject, exc, path.parent, isolated)
                return False

        try:
            moved = os.stat(isolated, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            if displaced is not None:
                displaced.add(isolated)
            log.error("%s: изолированный объект не читается: %s (%s); он лежит в %s под "
                      "именем %s", what, path, exc, path.parent, isolated)
            return False
        if _identity_of(moved) != identity:
            # Под нашим именем оказался ЧУЖОЙ объект. Он не удаляется ни при каких
            # обстоятельствах — либо возвращается, либо остаётся под именем изоляции.
            restored = "возвращён на место" if put_back("чужой объект") \
                else f"оставлен под именем {isolated}"
            log.warning("%s: перед снятием на этом имени оказался чужой объект; "
                        "он не удалён, %s: %s", what, restored, path)
            return False
        try:
            os.rmdir(isolated, dir_fd=parent)
        except OSError as exc:
            # Например, каталог не пуст: часть содержимого намеренно не трогали.
            put_back("свой каталог")
            log.warning("%s снять не удалось целиком, каталог оставлен: %s (%s)",
                        what, path, exc)
            return False
        # stage-16, EVA-14: снятие всё равно шло ПО ИМЕНИ, и в окно между сверкой и
        # ``rmdir`` изолированный свой каталог можно увести, подставив на это имя чужой
        # пустой. Закреплённый дескриптор отвечает, ЧТО именно исчезло: у снятого каталога
        # ссылок не остаётся. Ненулевое число — признак, что снят был не наш объект.
        alive = os.fstat(pinned_fd).st_nlink
        if alive:
            log.error("%s: под именем изоляции %s снят НЕ наш каталог — наш ещё существует "
                      "(ссылок на него %d) и уведён из %s под неизвестным именем. Очистка "
                      "НЕ состоялась: %s", what, isolated, alive, path.parent, path)
            return False
    finally:
        os.close(parent)
    return True


def _remove_windows_entry(entry: os.DirEntry, info: os.stat_result) -> None:
    """Снимает одну запись внутри УЖЕ закреплённого каталога.

    Точка повторного разбора снимается как запись: ни ``DeleteFileW``, ни
    ``RemoveDirectoryW`` не идут по ней в цель. Обычный каталог закрепляется своим
    описателем и снимается рекурсивно — так подмена ЛЮБОГО звена ниже тоже исключена.
    """
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    linked = bool(getattr(info, "st_file_attributes", 0) & reparse
                  or getattr(info, "st_reparse_tag", 0) or stat.S_ISLNK(info.st_mode))
    if linked:
        (os.rmdir if stat.S_ISDIR(info.st_mode) else os.unlink)(entry.path)
        return
    if not stat.S_ISDIR(info.st_mode):
        # Удаление файла по имени не разбирает последнее звено: подменившая его ссылка
        # снимется сама, а не её цель.
        os.unlink(entry.path)
        return
    child = winfs.pin_directory(Path(entry.path))
    try:
        if child.is_reparse_point():
            # Каталог подменили соединением между чтением записи и открытием описателя:
            # снимаем САМО соединение по описателю, внутрь не заходим.
            child.delete()
            return
        _empty_pinned_windows(Path(entry.path))
        child.delete()
    finally:
        child.close()


def _empty_pinned_windows(path: Path, protect: frozenset[str] = frozenset()) -> None:
    """Снимает содержимое каталога, закреплённого вызывающим.

    Пока описатель открыт, путь ``path`` разбирается в тот же объект: ни одно звено до него
    подменить нельзя. Поэтому обход идёт по путям, но каждая ВЛОЖЕННАЯ запись проверяется
    заново — внутрь своего каталога писать может кто угодно.
    """
    with os.scandir(path) as listing:
        entries = list(listing)  # перечисление закрывается ДО удаления: открытый обход держал бы каталог
    for entry in entries:
        if entry.name in protect:
            # stage-14, S4b: принадлежность этой записи уже отклонена — не трогаем.
            log.warning("Запись не снимается: её принадлежность уже отклонена: %s", entry.name)
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        _remove_windows_entry(entry, info)


def _discard_pinned_windows(path: Path, identity: Identity, what: str,
                            protect: frozenset[str] = frozenset()) -> bool:
    """Windows: каталог закрепляется описателем и снимается по нему же, а не по пути."""
    try:
        pinned = winfs.pin_directory(path)
    except OSError as exc:
        log.warning("%s закрепить не удалось, очистка отменена: %s (%s)", what, path, exc)
        return False
    try:
        if pinned.is_reparse_point() or _identity_of(pinned.stat()) != identity:
            log.warning("%s перестал быть своим, очистка отменена: %s", what, path)
            return False
        _empty_pinned_windows(path, protect)
        pinned.delete()
    finally:
        pinned.close()
    return True


def discard_inside_owned_directory(parent: Path, identity: Identity | None, name: str,
                                   what: str) -> bool:
    """Снимает ОДНУ запись внутри своего каталога, закрепив сам каталог.

    Владение здесь подтверждает РОДИТЕЛЬ: каталог версии создаёт бэкенд или плагин, а вот
    каталог выгрузки ``run-<uuid>`` создал этот прогон и удостоверение снял при создании.
    Закрепление родителя делает путь ``parent/name`` неподменяемым на всё время удаления:
    в POSIX всё идёт относительно дескриптора, на Windows описатель держит и родителя, и
    его предков. Отдельного удостоверения у записи нет — и не нужно: удаляется содержимое
    каталога, которым прогон владеет целиком.
    """
    if identity is None or os.sep in name or (os.altsep and os.altsep in name) or name in (
            "", ".", ".."):
        log.warning("%s снять нельзя: нет подтверждённого владения каталогом %s", what, parent)
        return False
    try:
        if _WINDOWS_PINNED:
            return _discard_entry_pinned_windows(parent, identity, name, what)
        if _PINNED_REMOVAL:
            return _discard_entry_pinned_posix(parent, identity, name, what)
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("%s снять не удалось: %s (%s)", what, parent / name, exc)
        return False
    log.warning("%s снять безопасно нечем: платформа не закрепляет каталог за объектом, "
                "очистка отменена: %s", what, parent / name)
    return False


def _discard_entry_pinned_posix(parent: Path, identity: Identity, name: str, what: str) -> bool:
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if _identity_of(os.fstat(fd)) != identity:
            log.warning("%s: каталог %s перестал быть своим, очистка отменена", what, parent)
            return False
        try:
            info = os.lstat(name, dir_fd=fd)
        except FileNotFoundError:
            return True
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)
    return True


def _discard_entry_pinned_windows(parent: Path, identity: Identity, name: str, what: str) -> bool:
    try:
        pinned = winfs.pin_directory(parent)
    except OSError as exc:
        log.warning("%s: каталог %s закрепить не удалось, очистка отменена (%s)", what, parent, exc)
        return False
    try:
        if pinned.is_reparse_point() or _identity_of(pinned.stat()) != identity:
            log.warning("%s: каталог %s перестал быть своим, очистка отменена", what, parent)
            return False
        target = parent / name
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return True
        _remove_windows_entry(_Entry(target), info)
    finally:
        pinned.close()
    return True


class _Entry:
    """Минимальная замена ``os.DirEntry``: удаление знает только путь записи."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)


def discard_owned_directory(path: Path, identity: Identity | None, what: str,
                            protect: frozenset[str] | set[str] | None = None,
                            displaced: set[str] | None = None) -> bool:
    """Снимает каталог, ТОЛЬКО если на этом пути тот же объект, который создал запуск.

    ``True`` — каталога больше нет (снят или его уже не было). ``False`` — очистка не
    состоялась: путь занят чужим объектом либо удаление не прошло целиком. Отказ громкий,
    но не исключение: очистка идёт в ``finally`` завершающегося прогона, и потеря чужих
    данных хуже, чем оставленный каталог.

    Удаление всегда идёт по ЗАКРЕПЛЁННОМУ объекту: дескриптор каталога в POSIX, описатель
    без ``FILE_SHARE_DELETE`` на Windows. Платформа, где закрепить каталог нечем, получает
    отказ от очистки, а не удаление по пути: оставленный каталог дешевле уничтоженного
    чужого дерева.

    ``displaced`` — необязательное множество, куда складываются имена объектов,
    оставшихся в РОДИТЕЛЬСКОМ каталоге под временным именем изоляции: вернуть их на
    исходное имя не удалось, потому что оно занято, а замещать занятое нельзя
    (stage-15, EVA-13). Вызывающий обязан защитить эти имена от своей же последующей
    уборки родителя — иначе объект, только что признанный чужим, будет снесён.

    ``protect`` — имена верхнего уровня, которые снимать НЕЛЬЗЯ ни при каких условиях
    (stage-14, S4b). Вызывающий передаёт сюда записи, принадлежность которых он уже
    отклонил: признать объект чужим и тут же снести его вместе с родителем — это
    противоречие, а не уборка. Защищённая запись оставляет каталог непустым, поэтому
    снятие самого каталога честно не состоится и вернёт ``False``.
    """
    path = Path(path)
    guard = frozenset(protect or ())
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True  # каталог уже снят: повторная очистка ничего не ищет и не тревожит
    except OSError as exc:
        log.warning("%s прочитать не удалось, очистка отменена: %s (%s)", what, path, exc)
        return False
    if identity is None or _identity_of(info) != identity:
        # Подмена ссылкой, соединением NTFS или другим настоящим каталогом: удаление ушло бы
        # в чужое дерево. Отказ от очистки безопаснее очистки не своего каталога.
        log.warning("%s перестал быть своим, очистка отменена: %s", what, path)
        return False
    try:
        if _WINDOWS_PINNED:
            return _discard_pinned_windows(path, identity, what, guard)
        if _PINNED_REMOVAL:
            return _discard_pinned_posix(path, identity, what, guard, displaced)
    except FileNotFoundError:
        return True  # каталог исчез по дороге — цель всё равно достигнута
    except OSError as exc:
        log.warning("%s снять не удалось целиком: %s (%s)", what, path, exc)
        return False
    log.warning("%s снять безопасно нечем: платформа не закрепляет каталог за объектом, "
                "очистка отменена: %s", what, path)
    return False


def reject_linked_path(path: str | Path) -> None:
    """Reject links/reparse points in the lexical target and every existing ancestor.

    Do not resolve first: that hides the link. lstat supports NTFS junctions on 3.11.
    This is a preflight containment check, not a filesystem TOCTOU lock.
    """
    target = Path(path).absolute()
    for item in [target, *target.parents]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or (os.name == 'nt' and (
                    info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or info.st_reparse_tag))):
            raise UnsafePathError('Linked transaction path')


def safe_join(root: str | Path, relative: str | PurePath) -> Path:
    """Возвращает путь внутри ``root`` или падает с :class:`UnsafePathError`."""
    root_path = Path(root)
    candidate = PurePath(relative)
    if candidate.is_absolute() or (os.name == "nt" and PurePath(str(relative)).drive):
        raise UnsafePathError(f"Абсолютный путь внутри выгрузки недопустим: {relative}")

    root_resolved = os.path.normcase(os.path.normpath(os.path.abspath(str(root_path))))
    target = os.path.normcase(os.path.normpath(os.path.abspath(str(root_path / candidate))))
    if target != root_resolved and not target.startswith(root_resolved + os.sep):
        raise UnsafePathError(f"Путь <{relative}> выходит за пределы каталога <{root_path}>")
    return Path(os.path.normpath(str(root_path / candidate)))
