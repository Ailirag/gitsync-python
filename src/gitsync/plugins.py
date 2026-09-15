"""Минимальный практичный API плагинов.

Плагин — обычный Python-модуль с функцией ``register(host)`` либо объект с методами-хуками.
Callbacks сериализованы; export hooks работают в worker threads, остальные в writer.
Исключения по умолчанию останавливают синхронизацию; контексты изменяют реальные данные.

ГРАНИЦЫ СОВМЕСТИМОСТИ: плагины upstream написаны на OneScript и используют подписки
``МенеджерПодписок`` (более 30 событий с изменяемыми параметрами ``СтандартнаяОбработка``).
Здесь реализованы ключевые события с data-oriented overrides и приоритетами — см.
docs/plugins.md. Загрузка .os-плагинов не поддерживается и не планируется в этой итерации.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock
from types import SimpleNamespace

log = logging.getLogger("gitsync.plugins")

#: События, которые гарантированно вызываются ядром.
SUPPORTED_EVENTS = (
    "before_sync", "after_sync", "before_export", "after_export", "before_cleanup",
    "before_history",
    "after_history",  # (history, current_version)
    "before_commit",  # (version, work_dir, author)
    "after_commit",  # (version, work_dir, sha)
)


@dataclass
class PluginHost:
    """Реестр плагинов и диспетчер событий."""

    handlers: dict[str, list[tuple[int, bool, Callable]]] = field(default_factory=dict)
    _lock: object = field(default_factory=RLock, repr=False)
    names: list[str] = field(default_factory=list)
    strict: bool = True

    def subscribe(self, event: str, handler: Callable[..., None], *,
                  priority: int = 0, contextual: bool = False) -> None:
        if event not in SUPPORTED_EVENTS:
            raise ValueError(
                f"Событие <{event}> не поддерживается. Доступны: {', '.join(SUPPORTED_EVENTS)}"
            )
        self.handlers.setdefault(event, []).append((priority, contextual, handler))
        self.handlers[event].sort(key=lambda item: item[0], reverse=True)

    def emit(self, event: str, *, context_values: dict | None = None, **payload) -> SimpleNamespace:
        context = SimpleNamespace(**{**payload, **(context_values or {})})
        with self._lock:
            for _, contextual, handler in self.handlers.get(event, ()):
                try:
                    if contextual:
                        handler(context)
                    else:
                        handler(**{key: getattr(context, key) for key in payload})
                except Exception as exc:  # noqa: BLE001
                    if self.strict or contextual:
                        raise
                    log.warning("Плагин на событии <%s> завершился с ошибкой: %s", event, exc)
        return context

    def load_module(self, module_name: str) -> None:
        module = importlib.import_module(module_name)
        register = getattr(module, "register", None)
        if register is None:
            raise ValueError(f"В модуле плагина <{module_name}> нет функции register(host)")
        register(self)
        self.names.append(module_name)
        log.info("Загружен плагин %s", module_name)

    def load_all(self, module_names: list[str]) -> None:
        for name in module_names:
            self.load_module(name)
