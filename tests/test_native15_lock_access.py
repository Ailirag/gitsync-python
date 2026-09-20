"""Native15: отказ по правам на файл блокировки обязан быть объяснён.

НАБЛЮДЕНО В КОНТЕЙНЕРАХ. Два контейнера, общий том блокировок, разные UID:
держатель (UID 10001) берёт блокировку, второй (UID 12345, группа 0) получает

    PermissionError: [Errno 13] Permission denied:
    '/var/lib/gitsync/sessions/d13f6ec6….lock'

и трассировку Python. Сам отказ ПРАВИЛЬНЫЙ и защитный: файл блокировки создаётся
с правами 0600, поэтому чужой UID не может ни взять её, ни обойти — два процесса
в одно хранилище одним логином не пойдут. Но оператору по такой трассировке
непонятно ни что случилось, ни что делать, а именно этот сценарий описан в
руководстве как «отказ, а не обход».

Здесь закрепляется: причина отказа называется словами, путь виден, и ошибка
принадлежит домену инструмента (её ловят и пакетный режим, и CLI), а не утекает
наружу как необработанное исключение ОС.
"""

from __future__ import annotations

import os

import pytest

from gitsync.errors import GitSyncError, LockBusyError
from gitsync.locks import exclusive_lock

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="запрет на чтение файла владельцем в Windows так не воспроизводится; в образе это Linux",
)


def test_unreadable_lock_file_reports_reason_and_path(tmp_path):
    """Нет прав на файл блокировки — понятная ошибка домена, а не PermissionError."""
    lock = tmp_path / "хранилище.lock"
    lock.write_text("pid=1 host=чужой\n", encoding="utf-8")
    lock.chmod(0o000)

    try:
        with pytest.raises(GitSyncError) as excinfo:
            with exclusive_lock(lock, timeout=1):
                pass
    finally:
        lock.chmod(0o600)

    message = str(excinfo.value)
    assert str(lock) in message, "в сообщении должен быть путь к файлу блокировки"
    assert "прав" in message.lower(), f"причина не названа: {message}"


def test_permission_refusal_is_not_reported_as_busy(tmp_path):
    """«Нет прав» и «занято» — разные вещи: их нельзя путать в диагностике."""
    lock = tmp_path / "хранилище.lock"
    lock.write_text("pid=1 host=чужой\n", encoding="utf-8")
    lock.chmod(0o000)

    try:
        with pytest.raises(GitSyncError) as excinfo:
            with exclusive_lock(lock, timeout=1):
                pass
    finally:
        lock.chmod(0o600)

    assert not isinstance(excinfo.value, LockBusyError), (
        "отказ по правам нельзя выдавать за занятость: ожидание не поможет"
    )


def test_message_tells_how_to_run_containers(tmp_path):
    """Подсказка обязана быть действием, а не констатацией."""
    lock = tmp_path / "хранилище.lock"
    lock.write_text("pid=1 host=чужой\n", encoding="utf-8")
    lock.chmod(0o000)

    try:
        with pytest.raises(GitSyncError) as excinfo:
            with exclusive_lock(lock, timeout=1):
                pass
    finally:
        lock.chmod(0o600)

    message = str(excinfo.value).lower()
    assert "uid" in message, f"надо назвать причину (чужой UID) и выход: {message}"


def test_normal_lock_still_works(tmp_path):
    """Обычный путь не задет: блокировка берётся и освобождается."""
    lock = tmp_path / "обычная.lock"
    with exclusive_lock(lock, timeout=1) as held:
        assert held == lock
    with exclusive_lock(lock, timeout=1):
        pass
