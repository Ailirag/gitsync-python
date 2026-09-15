"""Минимальный практичный API плагинов.

Плагин — обычный Python-модуль с функцией ``register(host)`` либо объект с методами-хуками.
Хуки вызываются синхронно из потока коммитов; исключение в плагине по умолчанию не валит
синхронизацию (пишется предупреждение), если хук не помечен как критичный.

ГРАНИЦЫ СОВМЕСТИМОСТИ: плагины upstream написаны на OneScript и используют подписки
``МенеджерПодписок`` (более 30 событий с изменяемыми параметрами ``СтандартнаяОбработка``).
Здесь реализовано подмножество событий без подмены стандартной обработки — см.
docs/plugins.md. Загрузка .os-плагинов не поддерживается и не планируется в этой итерации.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("gitsync.plugins")

#: События, которые гарантированно вызываются ядром.
SUPPORTED_EVENTS = (
    "after_history",  # (history, current_version)
    "before_commit",  # (version, work_dir, author)
    "after_commit",  # (version, work_dir, sha)
)


@dataclass
class PluginHost:
    """Реестр плагинов и диспетчер событий."""

    handlers: dict[str, list[Callable[..., None]]] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)
    strict: bool = False

    def subscribe(self, event: str, handler: Callable[..., None]) -> None:
        if event not in SUPPORTED_EVENTS:
            raise ValueError(
                f"Событие <{event}> не поддерживается. Доступны: {', '.join(SUPPORTED_EVENTS)}"
            )
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, **payload) -> None:
        for handler in self.handlers.get(event, ()):
            try:
                handler(**payload)
            except Exception as exc:  # noqa: BLE001 — плагин не должен валить синхронизацию
                if self.strict:
                    raise
                log.warning("Плагин на событии <%s> завершился с ошибкой: %s", event, exc)

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
