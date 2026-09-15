"""Типы ошибок gitsync-py."""

from __future__ import annotations


class GitSyncError(Exception):
    """Базовая ошибка."""


class DirtyWorkingCopyError(GitSyncError):
    """В рабочей копии есть незафиксированные изменения пользователя."""


class LockBusyError(GitSyncError):
    """Целевой репозиторий уже обрабатывается другим процессом."""


class DesignerError(GitSyncError):
    """Конфигуратор 1С вернул ошибку."""


class DesignerTimeoutError(DesignerError):
    """Конфигуратор не уложился в отведённое время."""


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
