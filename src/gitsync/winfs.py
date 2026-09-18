"""Закрепление каталога Windows за конкретным объектом файловой системы.

В POSIX каталог закрепляется дескриптором (``O_DIRECTORY | O_NOFOLLOW``), и дальше всё
удаляется ОТНОСИТЕЛЬНО него: подмена пути после проверки уже никуда не уводит. В стандартной
библиотеке Windows такого примитива нет, но он есть в самой системе, и опирается он на
правила совместного доступа, а не на догадки о путях.

Описатель каталога, открытый БЕЗ ``FILE_SHARE_DELETE``, запрещает остальным переименовать
или удалить этот каталог — и, что важнее, любого его ПРЕДКА. Измерено на стенде
(Windows 11, NTFS, Python 3.11.9), из отдельного процесса:

* переименовать сам закреплённый каталог — ``ERROR_SHARING_VIOLATION`` (32);
* переименовать его родителя — ``ERROR_ACCESS_DENIED`` (5);
* переименовать каталог двумя уровнями выше — ``ERROR_ACCESS_DENIED`` (5).

Пока описатель открыт, путь к закреплённому каталогу разбирается в тот же объект: ни одно
звено пути подменить нельзя. Сам каталог снимается затем ПО ОПИСАТЕЛЮ
(``FileDispositionInfoEx``), то есть путь в удалении не участвует вовсе.

Удостоверение снимается с ОТКРЫТОГО описателя (``os.fstat``) — тем же способом, каким его
снимали при создании (``os.lstat``), поэтому пары «устройство, индекс» сравнимы напрямую.
``os.fstat`` не видит точку повторного разбора (тег в ``GetFileInformationByHandle`` не
приходит), поэтому «это не junction» проверяется отдельным запросом
``FileAttributeTagInfo`` — тоже по описателю.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Модуль импортируется НА ЛЮБОЙ платформе: :mod:`gitsync.safepath` тянет его безусловно,
#: а ``msvcrt``/``ctypes.wintypes`` есть только в Windows. Всё, что к ним обращается,
#: создаётся внутри этой ветки; на прочих платформах модуль остаётся пустой заглушкой с
#: ``AVAILABLE = False``.
_WINDOWS = os.name == "nt"

if _WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

_DELETE = 0x00010000
_FILE_LIST_DIRECTORY = 0x0001
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FileAttributeTagInfo = 9
_FileDispositionInfo = 4
_FileDispositionInfoEx = 21
_FLAG_DELETE = 0x01
_FLAG_POSIX_SEMANTICS = 0x02

_ERROR_INVALID_PARAMETER = 87
_ERROR_NOT_SUPPORTED = 50
_ERROR_CALL_NOT_IMPLEMENTED = 120
_ERROR_INVALID_FUNCTION = 1


if _WINDOWS:
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class _FileAttributeTagInfoStruct(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    class _FileDispositionInfoExStruct(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD)]

    class _FileDispositionInfoStruct(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_ubyte)]


def _kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateFileW.restype = wintypes.HANDLE
    library.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
    library.GetFileInformationByHandleEx.restype = wintypes.BOOL
    library.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD]
    library.SetFileInformationByHandle.restype = wintypes.BOOL
    library.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
    library.CloseHandle.restype = wintypes.BOOL
    library.CloseHandle.argtypes = [wintypes.HANDLE]
    return library


_K32 = _kernel32() if _WINDOWS else None

#: Закрепление доступно только там, где есть Win32 и ``msvcrt``.
AVAILABLE = _K32 is not None


class PinnedDirectory:
    """Открытый каталог: пока он не закрыт, ни он сам, ни его предки не переименовываются."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self.fd = fd

    @property
    def _handle(self) -> int:
        return msvcrt.get_osfhandle(self.fd)

    def stat(self) -> os.stat_result:
        """``stat`` закреплённого ОБЪЕКТА, а не того, что сейчас лежит на пути."""
        return os.fstat(self.fd)

    def attributes(self) -> tuple[int, int]:
        """``(атрибуты, тег повторного разбора)`` по описателю."""
        info = _FileAttributeTagInfoStruct()
        ok = _K32.GetFileInformationByHandleEx(self._handle, _FileAttributeTagInfo,
                                               ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            raise OSError(0, "GetFileInformationByHandleEx", str(self.path),
                          ctypes.get_last_error())
        return info.FileAttributes, info.ReparseTag

    def is_reparse_point(self) -> bool:
        """Junction, символьная ссылка и прочие точки повторного разбора.

        ``os.fstat`` их не различает: тег приходит только отдельным запросом. Без этой
        проверки соединение NTFS, подменившее каталог, выглядело бы обычным каталогом.
        """
        attributes, tag = self.attributes()
        return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) or bool(tag)

    def is_directory(self) -> bool:
        attributes, _ = self.attributes()
        return bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)

    def delete(self) -> None:
        """Снимает ЗАКРЕПЛЁННЫЙ объект по описателю; путь в удалении не участвует.

        Каталог должен быть пуст — как и у ``rmdir``. Для точки повторного разбора
        снимается сама точка: её цель системой не затрагивается.
        """
        extended = _FileDispositionInfoExStruct(_FLAG_DELETE | _FLAG_POSIX_SEMANTICS)
        if _K32.SetFileInformationByHandle(self._handle, _FileDispositionInfoEx,
                                           ctypes.byref(extended), ctypes.sizeof(extended)):
            return
        error = ctypes.get_last_error()
        if error not in (_ERROR_INVALID_PARAMETER, _ERROR_NOT_SUPPORTED,
                         _ERROR_CALL_NOT_IMPLEMENTED, _ERROR_INVALID_FUNCTION):
            raise OSError(0, "SetFileInformationByHandle(FileDispositionInfoEx)",
                          str(self.path), error)
        # Старая система или не-NTFS: пометка «удалить при закрытии» — тоже по описателю.
        legacy = _FileDispositionInfoStruct(1)
        if not _K32.SetFileInformationByHandle(self._handle, _FileDispositionInfo,
                                               ctypes.byref(legacy), ctypes.sizeof(legacy)):
            raise OSError(0, "SetFileInformationByHandle(FileDispositionInfo)",
                          str(self.path), ctypes.get_last_error())

    def close(self) -> None:
        os.close(self.fd)


def pin_directory(path: Path) -> PinnedDirectory:
    """Открывает каталог так, что его нельзя ни переименовать, ни подменить, ни удалить.

    ``FILE_FLAG_OPEN_REPARSE_POINT`` — чтобы открыть САМО последнее звено пути, а не то,
    куда оно ведёт: иначе соединение NTFS увело бы описатель в чужое дерево ещё до всякой
    проверки. Режим совместного доступа без ``FILE_SHARE_DELETE`` и есть закрепление.
    """
    if _K32 is None:  # pragma: no cover — модуль используется только на Windows
        raise OSError("Закрепление каталога доступно только на Windows")
    handle = _K32.CreateFileW(str(path), _DELETE | _FILE_LIST_DIRECTORY | _SYNCHRONIZE,
                              _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING,
                              _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle == _INVALID_HANDLE_VALUE:
        raise OSError(0, "CreateFileW", str(path), ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        _K32.CloseHandle(wintypes.HANDLE(handle))
        raise
    return PinnedDirectory(Path(path), fd)
