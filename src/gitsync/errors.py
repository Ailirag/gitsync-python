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
