"""Типы ошибок gitsync-py."""

from __future__ import annotations


class GitSyncError(Exception):
    """Базовая ошибка."""


class DirtyWorkingCopyError(GitSyncError):
    """В рабочей копии есть незафиксированные изменения пользователя."""


class LockBusyError(GitSyncError):
    """Целевой репозиторий уже обрабатывается другим процессом."""


class LockAccessError(GitSyncError):
    """Файл блокировки недоступен (создан другим UID) — это отказ, а не занятость.

    Отделено от :class:`LockBusyError` намеренно: занятость проходит сама, а отказ по
    правам ожиданием не лечится, и повторять попытку бессмысленно.
    """


class DesignerError(GitSyncError):
    """Конфигуратор 1С вернул ошибку."""


class DesignerTimeoutError(DesignerError):
    """Конфигуратор не уложился в отведённое время."""


class LicenseUnavailableError(DesignerError):
    """Платформа не получила лицензию на ЭТОТ запуск — работа не начиналась.

    Отделено от :class:`DesignerError` намеренно, по той же причине, по какой
    :class:`LockAccessError` отделён от :class:`LockBusyError`: это отказ В ВЫДАЧЕ
    лицензии, а не результат работы. Хранилище не читалось, ИБ не менялась,
    повторный запрос ничего не портит и на стенде проходит сам (evidence/stage-19).

    Обратное неверно: неверный пользователь хранилища, отсутствующая версия и
    небезопасный путь ожиданием не лечатся и к этому типу не относятся.
    """


class StorageVersionMismatchError(GitSyncError):
    """Версия в git больше версии в хранилище (хранилище могли пересоздать/обрезать)."""


class UnsafePathError(GitSyncError):
    """Путь выгрузки выходит за пределы рабочей копии или ведёт по символьной ссылке."""


class CancelledError(GitSyncError):
    """Выполнение отменено пользователем."""


class ExportIncompleteError(GitSyncError):
    """Бэкенд не подтвердил выгрузку версии: каталога нет или он пуст без явного разрешения."""


class VersionFileError(GitSyncError):
    """Файл VERSION отсутствует или не читается — продолжать нельзя (см. init/set-version)."""


class PostCommitError(GitSyncError):
    """Коммит уже создан, но обработчик после коммита завершился с ошибкой."""


class ConfigError(GitSyncError):
    """Ошибка в файле конфигурации пакетного режима."""
