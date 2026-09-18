"""Review-03 regressions; real local Git and owned Windows scratch only."""
import os
import stat
import subprocess

import pytest

from gitsync.errors import UnsafePathError
from gitsync.transaction import owned_path
from gitsync.version_file import read_version_file
from test_safety_recovery import _backend, _manager, _seed


@pytest.mark.parametrize('name', ['.gitignore', '.gitattributes'])
@pytest.mark.parametrize('existing', [False, True])
def test_file_only_service_directory_rejected_before_worktree_write(tmp_path, name, existing):
    work = tmp_path / 'repo'
    repo = _seed(work, 0, {'object.txt': 'old'})
    if existing:
        (work / name).mkdir()
    before = (work / 'VERSION').read_bytes(), repo.head_sha(), (work / 'object.txt').read_bytes()
    backend = _backend(1)
    backend.files = {1: {f'{name}/nested.txt': 'bad', 'object.txt': 'new'}}
    result = _manager(work, backend).sync(raise_on_error=False)
    assert isinstance(result.error, UnsafePathError)
    assert ((work / 'VERSION').read_bytes(), repo.head_sha(), (work / 'object.txt').read_bytes()) == before
    assert not (work / name / 'nested.txt').exists()
    assert not (work / '.git/gitsync-py-journal.json').exists()


@pytest.mark.skipif(os.name != 'nt', reason='actual NTFS junction requires Windows')
@pytest.mark.parametrize('name', ['VERSION', 'nested/VERSION'])
def test_actual_junction_target_and_parent_rejected(tmp_path, name):
    work = tmp_path / 'repo'
    work.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'VERSION'
    sentinel.write_bytes(b'OUTSIDE')
    link = work / name.split('/')[0]
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                            capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        info = link.lstat()
        assert info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        assert info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
        with pytest.raises(UnsafePathError, match='Linked'):
            owned_path(work, name)
        assert sentinel.read_bytes() == b'OUTSIDE'
    finally:
        os.rmdir(link)


def test_set_version_noncommit_remains_explicit_resume_marker(tmp_path):
    work = tmp_path / 'repo'
    repo = _seed(work, 0, {'object.txt': 'old'})
    head = repo.head_sha()
    index = repo.run(['ls-files', '--stage', '-z']).stdout
    lock = work / '.git/index.lock'
    lock.write_bytes(b'OTHER OWNER')
    try:
        assert _manager(work, _backend(1)).set_version(1, commit=False, raise_on_error=True)
        assert lock.read_bytes() == b'OTHER OWNER'
    finally:
        lock.unlink()
    assert read_version_file(work) == 1 and repo.head_sha() == head
    assert repo.run(['ls-files', '--stage', '-z']).stdout == index
    assert not (work / '.git/gitsync-py-journal.json').exists()
    assert _manager(work, _backend(1)).sync().committed == []


def test_set_version_occupied_index_preserves_marker_head_and_lock(tmp_path):
    work = tmp_path / 'repo'
    repo = _seed(work, 0, {'object.txt': 'old'})
    before = (work / 'VERSION').read_bytes(), repo.head_sha(), repo.run(['ls-files', '--stage', '-z']).stdout
    lock = work / '.git/index.lock'
    lock.write_bytes(b'OTHER GIT OWNER')
    try:
        with pytest.raises(FileExistsError):
            _manager(work, _backend(1)).set_version(1, commit=True, raise_on_error=True)
        assert lock.read_bytes() == b'OTHER GIT OWNER'
        assert ((work / 'VERSION').read_bytes(), repo.head_sha(),
                repo.run(['ls-files', '--stage', '-z']).stdout) == before
        assert not (work / '.git/gitsync-py-journal.json').exists()
    finally:
        lock.unlink()  # exclusively test-owned live lock, never an arbitrary stale lock
    assert _manager(work, _backend(1)).sync().committed == [1]


@pytest.mark.parametrize('phase', ['before-ref', 'after-ref'])
def test_set_version_real_process_exit_reconciles_content_bound_wal(tmp_path, phase):
    import json
    import sys
    from pathlib import Path

    import gitsync

    # Каталог импорта берётся у самого модуля: тест обязан проверять ТОТ код,
    # который импортирован, а не предполагать раскладку дерева исходников
    # (при установке колеса — в контейнере — исходников рядом нет).
    src = Path(gitsync.__file__).resolve().parent.parent
    assert (src / 'gitsync' / '__init__.py').is_file()
    work = tmp_path / 'repo'
    repo = _seed(work, 0, {'object.txt': 'old'})
    initial = repo.head_sha()
    code = '''import os, sys
from pathlib import Path
src = Path(sys.argv[1]); sys.path.insert(0, str(src))
import gitsync
assert Path(gitsync.__file__).resolve() == src / 'gitsync/__init__.py'
print('CHILD_IMPORT=' + gitsync.__file__, flush=True)
from gitsync.sync import SyncManager, SyncOptions
from gitsync.backends import FakeStorageBackend
m = SyncManager(Path(sys.argv[2]), FakeStorageBackend([]), SyncOptions(lock_timeout=0))
phase = sys.argv[3]; run = m.repo.run
def crash(args, **kwargs):
    if args[0] == 'update-ref' and phase == 'before-ref': os._exit(83)
    result = run(args, **kwargs)
    if args[0] == 'update-ref' and phase == 'after-ref': os._exit(83)
    return result
m.repo.run = crash
m.set_version(1, commit=True, raise_on_error=True)
raise AssertionError('crash seam not reached')
'''
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    child = subprocess.run([sys.executable, '-c', code, str(src), str(work), phase],
                           cwd=tmp_path, env=env, capture_output=True, text=True, timeout=40)
    assert child.returncode == 83, child.stderr
    assert 'CHILD_IMPORT=' + str(src / 'gitsync/__init__.py') in child.stdout
    journal = work / '.git/gitsync-py-journal.json'
    entry = json.loads(journal.read_text())
    lock = work / '.git/index.lock'
    assert lock.read_bytes() == ('gitsync:' + entry['lock_token']).encode()
    assert set(entry['files']) == {'VERSION'}
    crashed_head = repo.head_sha()
    restart = _manager(work, _backend(1)).sync()
    assert restart.ok and repo.is_clean() and repo.commit_count() == 2
    assert not journal.exists() and not lock.exists()
    assert read_version_file(work) == 1
    if phase == 'before-ref':
        assert crashed_head == initial and restart.rolled_back == [1] and restart.committed == [1]
    else:
        assert crashed_head == entry['sha'] == repo.head_sha() and restart.committed == []


@pytest.mark.parametrize('failure', ['journal-ack', 'index-publish'])
def test_set_version_postpublication_error_never_rolls_back_commit(tmp_path, monkeypatch, failure):
    from pathlib import Path

    import gitsync.sync as sm
    import gitsync.transaction as tx
    from gitsync.errors import PostCommitError

    work = tmp_path / 'repo'
    repo = _seed(work, 0, {'object.txt': 'old'})
    with monkeypatch.context() as patch:
        if failure == 'journal-ack':
            original = sm._Journal.write
            def fail(self, entry):
                if entry['state'] == 'committed':
                    raise OSError('ack journal unavailable')
                return original(self, entry)
            patch.setattr(sm._Journal, 'write', fail)
        else:
            original = tx.os.replace
            def fail(a, b):
                if Path(b) == work / '.git/index':
                    raise OSError('index publish unavailable')
                return original(a, b)
            patch.setattr(tx.os, 'replace', fail)
        with pytest.raises(PostCommitError):
            _manager(work, _backend(1)).set_version(1, commit=True, raise_on_error=True)
    head = repo.head_sha()
    assert read_version_file(work) == 1
    assert '<VERSION>1</VERSION>' in repo.run(['show', 'HEAD:VERSION']).stdout
    assert (work / '.git/gitsync-py-journal.json').exists()
    restart = _manager(work, _backend(1)).sync()
    assert restart.ok and restart.committed == [] and repo.head_sha() == head and repo.is_clean()
