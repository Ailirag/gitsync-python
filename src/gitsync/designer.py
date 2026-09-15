"""Вызов конфигуратора 1С:Предприятие.

Состав аргументов взят из библиотеки ``v8runner`` (её использует upstream gitsync):

* базовый набор — ``DESIGNER``, ключ соединения с ИБ, ``/Out <файл>``, ``/DisableStartupMessages``,
  ``/DisableStartupDialogs`` (``v8runner.СтандартныеПараметрыЗапускаКонфигуратора``);
* отчёт по версиям — ``/ConfigurationRepositoryF``, ``/ConfigurationRepositoryN``,
  ``/ConfigurationRepositoryP``, ``/ConfigurationRepositoryReport <файл>``, ``-NBegin``, ``-NEnd``
  (``ПолучитьОтчетПоВерсиямИзХранилища``);
* получение версии — ``/ConfigurationRepositoryUpdateCfg`` и **сразу после него** ``-v <номер>``,
  затем ``-force`` (в исходнике это отмечено как критичный порядок);
* выгрузка в файлы — ``/DumpConfigToFiles <каталог> -format Hierarchical`` и ``-Extension <имя>``
  либо ``-AllExtensions`` для расширений (``ВыгрузитьКонфигурациюВФайлы``/``ВыгрузитьРасширениеВФайлы``).

Отличия от upstream сделаны намеренно: аргументы передаются **массивом argv без shell**
(в oscript-версии строка склеивается и на Linux уходит в ``sh -c``), пароли маскируются
в логах и в тексте исключений.

НЕ ВЕРИФИЦИРОВАНО в этой итерации: фактический запуск на живой платформе 1С (нет стенда),
поэтому коды возврата/тексты ошибок конфигуратора разобраны только по исходникам.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .errors import DesignerError, DesignerTimeoutError

log = logging.getLogger("gitsync.designer")

DEFAULT_DESIGNER_TIMEOUT = 3600.0
MASK = "***"

# Ключи, значение которых нельзя показывать. Формы: "/P" + значение отдельным аргументом
# и "/Pзначение" одним аргументом (так делает v8runner).
_SECRET_PREFIXES = ("/ConfigurationRepositoryP", "/P")


@dataclass(frozen=True)
class StorageAccess:
    """Доступ к хранилищу конфигурации."""

    path: str
    user: str
    password: str = ""


def mask_secrets(args: list[str]) -> list[str]:
    """Маскирует пароли в argv для логов и сообщений об ошибках."""
    masked: list[str] = []
    mask_next = False
    for item in args:
        if mask_next:
            masked.append(MASK)
            mask_next = False
            continue
        matched = next((p for p in _SECRET_PREFIXES if item == p or item.startswith(p)), None)
        if matched is None:
            masked.append(item)
            continue
        if item == matched:
            masked.append(item)
            mask_next = True
        else:
            masked.append(matched + MASK)
    return masked


class DesignerRunner:
    """Сборка argv и запуск конфигуратора."""

    def __init__(
        self,
        v8_path: str,
        out_dir: str | Path,
        version: str = "",
        timeout: float = DEFAULT_DESIGNER_TIMEOUT,
        language: str = "RU",
    ):
        self.v8_path = v8_path
        self.out_dir = Path(out_dir)
        self.version = version
        self.timeout = timeout
        # Апстрим форсирует RU: выгрузка истории хранилища с другими языками даёт только RU-отчёт.
        self.language = language

    # --- сборка аргументов ---------------------------------------------

    def out_file(self) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self.out_dir / f"designer-out-{uuid.uuid4().hex}.log"

    def base_args(self, ib_connection: str, out_file: Path | None = None) -> list[str]:
        out = out_file or self.out_file()
        args = [self.v8_path, "DESIGNER", ib_connection, "/Out", str(out)]
        if self.language:
            args += [f"/L{self.language}"]
        args += ["/DisableStartupMessages", "/DisableStartupDialogs"]
        return args

    def _storage_args(self, access: StorageAccess) -> list[str]:
        args = [
            "/ConfigurationRepositoryF",
            access.path,
            "/ConfigurationRepositoryN",
            access.user,
        ]
        if access.password:
            args += ["/ConfigurationRepositoryP", access.password]
        return args

    def build_report_args(
        self,
        access: StorageAccess,
        report_path: str | Path,
        begin: int = 1,
        end: int | None = None,
        ib_connection: str = "",
        out_file: Path | None = None,
    ) -> list[str]:
        args = self.base_args(ib_connection, out_file)
        args += self._storage_args(access)
        args += ["/ConfigurationRepositoryReport", str(report_path), "-NBegin", str(begin)]
        if end:
            args += ["-NEnd", str(end)]
        return args

    def build_update_cfg_args(
        self,
        access: StorageAccess,
        version: int,
        ib_connection: str,
        out_file: Path | None = None,
    ) -> list[str]:
        args = self.base_args(ib_connection, out_file)
        args += self._storage_args(access)
        args += ["/ConfigurationRepositoryUpdateCfg"]
        # Порядок обязателен: без "-v" сразу после команды всегда приходит последняя версия.
        if version and version > 0:
            args += ["-v", str(version)]
        args += ["-force"]
        return args

    def build_dump_args(
        self,
        dump_dir: str | Path,
        ib_connection: str,
        extension: str | None = None,
        dump_format: str = "Hierarchical",
        out_file: Path | None = None,
    ) -> list[str]:
        args = self.base_args(ib_connection, out_file)
        args += ["/DumpConfigToFiles", str(dump_dir), "-format", dump_format]
        if extension:
            if extension == "-AllExtensions":
                args += ["-AllExtensions"]
            else:
                args += ["-Extension", extension]
        return args

    # --- запуск ---------------------------------------------------------

    def run(self, args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        safe = mask_secrets(args)
        log.debug("Запуск конфигуратора: %s", " ".join(safe))
        try:
            result = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DesignerTimeoutError(
                f"Конфигуратор не завершился за {timeout or self.timeout:g} с: {' '.join(safe)}"
            ) from exc
        if result.returncode != 0:
            raise DesignerError(
                f"Конфигуратор завершился с кодом {result.returncode}: {' '.join(safe)}\n"
                f"{(result.stderr or result.stdout or '').strip()[:2000]}"
            )
        return result
