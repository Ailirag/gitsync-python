"""Host/account-wide repository-login admission, independent of worker/TEMP roots.

Only repository Report/DumpCfg processes hold this lock. Private IB processing
never does. Cooperating processes under the same OS account share the namespace;
other hosts/accounts or external Designer clients require operational coordination.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .designer import StorageAccess


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
    return Path.home() / ".gitsync" / "repository-sessions" / (key + ".lock")
