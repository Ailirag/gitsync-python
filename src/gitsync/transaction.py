"""Write-ahead file preimages and content-checked recovery (never checkout/reset)."""
from __future__ import annotations

import base64
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from .errors import UnsafePathError


def owned_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or '\\' in name:
        raise UnsafePathError('Invalid transaction path')
    parts = PurePosixPath(name).parts
    if (PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive
            or any(p in {'.', '..'} or ':' in p or p.rstrip(' .') != p
                   or p.casefold() in {'.git', 'git~1', '.gitsync-py.lock'} for p in parts)):
        raise UnsafePathError('Unsafe transaction path')
    target = root.joinpath(*parts)
    for item in [root, *target.parents, target]:
        if item.is_symlink() or (item.exists() and getattr(item, 'is_junction', lambda: False)()):
            raise UnsafePathError('Linked transaction path')
    return target


def image(path: Path) -> dict | None:
    if path.is_symlink():
        raise UnsafePathError('Linked file cannot be owned')
    if not path.exists():
        return None
    if not path.is_file():
        raise UnsafePathError('Non-file transaction target')
    return {'data': base64.b64encode(path.read_bytes()).decode('ascii'),
            'mode': stat.S_IMODE(path.stat().st_mode)}


def put_image(path: Path, value: dict | None) -> None:
    if value is None:
        path.unlink(missing_ok=True)
        return
    data = base64.b64decode(value['data'], validate=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.gitsync-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, value['mode'])
        os.replace(name, path)  # detach hardlinks; never truncate an existing inode
    finally:
        Path(name).unlink(missing_ok=True)


def index_entries(repo, env=None) -> dict[str, str]:
    raw = repo.run(['ls-files', '--stage', '-z'], env=env).stdout
    entries = {}
    for row in raw.split('\0'):
        if row:
            meta, name = row.split('\t', 1)
            if not meta.endswith(' 0'):
                raise UnsafePathError('Unmerged index; manual recovery required')
            entries[name] = meta
    return entries


def blob_entry(repo, value: dict | None) -> str | None:
    if value is None:
        return None
    result = subprocess.run([repo.git_path, 'hash-object', '-w', '--stdin'],
                            cwd=repo.path, input=base64.b64decode(value['data']),
                            capture_output=True, check=True, timeout=repo.timeout)
    mode = '100755' if value['mode'] & 0o111 and os.name != 'nt' else '100644'
    return f'{mode} {result.stdout.decode().strip()} 0'


def update_entries(repo, entries: dict, env: dict) -> None:
    for name, value in entries.items():
        if value is None:
            repo.run(['update-index', '--force-remove', '--', name], env=env)
        else:
            mode, oid, _ = value.split()
            repo.run(['update-index', '--add', '--cacheinfo', mode, oid, name], env=env)


@contextmanager
def locked_index(repo, token: str | None = None):
    """Hold Git's real index.lock; edit a private copy, publish only on success."""
    gitdir = Path(repo.run(['rev-parse', '--absolute-git-dir']).stdout.strip())
    index = gitdir / 'index'
    lock = gitdir / 'index.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(('gitsync:' + token).encode() if token else b'')
        handle.flush()
        os.fsync(handle.fileno())
    fd, name = tempfile.mkstemp(prefix='gitsync-index-', dir=gitdir)
    os.close(fd)
    temp = Path(name)
    try:
        if index.exists():
            temp.write_bytes(index.read_bytes())
        else:
            temp.unlink()
        env = {'GIT_INDEX_FILE': str(temp)}
        yield env
        if temp.exists():
            with open(temp, 'r+b') as handle:
                os.fsync(handle.fileno())
            os.replace(temp, index)
    finally:
        temp.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
