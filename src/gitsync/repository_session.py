"""Host/account-wide repository-login admission, independent of worker/TEMP roots.

Only repository Report/DumpCfg processes hold this lock. Private IB processing
never does. Cooperating processes under the same OS account share the namespace;
other hosts/accounts or external Designer clients require operational coordination.

В контейнере домашний каталог — это не «одно место на машину»: у каждого контейнера
свой HOME, а у compose-проектов ещё и разные тома. Поэтому каталог задаётся явно
переменной окружения ``GITSYNC_SESSION_DIR``: она указывает на ОДИН общий том,
подключённый ко всем сотрудничающим контейнерам одного хоста под ОДНИМ UID.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .designer import StorageAccess
from .errors import ConfigError

#: Явный каталог общих блокировок сессий хранилища (общий том контейнеров).
SESSION_DIR_ENV = "GITSYNC_SESSION_DIR"

_NO_HOME = (
    f"Не удалось определить домашний каталог для файлов блокировки сессий хранилища. "
    f"Так бывает в контейнере, запущенном с произвольным UID без записи в /etc/passwd. "
    f"Задайте переменную окружения {SESSION_DIR_ENV} с абсолютным путём к общему каталогу "
    f"(один том для всех контейнеров одного хоста) либо задайте HOME на доступный для "
    f"записи каталог."
)


def repository_session_root() -> Path:
    """Каталог блокировок: явный ``GITSYNC_SESSION_DIR`` или ``~/.gitsync/repository-sessions``.

    Отказ здесь намеренно ранний и понятный: неработающая блокировка обнаружилась бы
    иначе только в момент, когда два конфигуратора уже вошли в хранилище под одним логином.
    """
    override = os.environ.get(SESSION_DIR_ENV, "").strip()
    if override:
        root = Path(override)
        if not root.is_absolute():
            raise ConfigError(
                f"{SESSION_DIR_ENV}=<{override}> должен быть абсолютным путём: относительный "
                "путь зависит от текущего каталога процесса, и контейнеры разошлись бы "
                "по разным файлам блокировки."
            )
    else:
        try:
            home = Path.home()
        except (RuntimeError, OSError) as exc:
            raise ConfigError(_NO_HOME) from exc
        if str(home).startswith("~"):  # pragma: no cover — подстраховка на старых платформах
            raise ConfigError(_NO_HOME)
        root = home / ".gitsync" / "repository-sessions"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(
            f"Каталог блокировок сессий хранилища <{root}> недоступен для создания/записи: {exc}. "
            f"Подключите том с правом записи для текущего UID и укажите его в {SESSION_DIR_ENV}."
        ) from exc
    return root


def repository_session_path(access: StorageAccess) -> Path:
    """Canonical local path (incl. aliases/case), or normalized server URI + login.

    Password, extension, worker directory and version are deliberately NOT keys.
    URI host aliases and mapped-drive/UNC aliases cannot be inferred reliably.
    """
    if "://" in access.path:
        uri = urlsplit(access.path.replace("\\", "/"))
        storage = urlunsplit((uri.scheme.lower(), uri.netloc.lower(),
                             uri.path.rstrip("/").casefold(), uri.query, ""))
    else:
        storage = os.path.normcase(str(Path(access.path).resolve()))
    identity = json.dumps([storage, access.user.casefold()], ensure_ascii=False).encode("utf-8")
    key = hashlib.sha256(identity).hexdigest()
    # Do not use tempfile: each CLI may set its own TEMP, splitting admission.
    return repository_session_root() / (key + ".lock")
