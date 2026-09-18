"""Review-02 transaction regressions: real Git, scratch-only, no network/native."""
import os
import subprocess
import sys
from pathlib import Path

from gitsync.version_file import read_version_file, write_version_file
from test_safety_recovery import _backend, _manager, _seed


def test_crash_after_ref_publication_recovers_index_without_reverting_commit(tmp_path):
    work = tmp_path / 'repo'
    repo = _seed(work, 0, {'object.txt': 'old'})
    code = '''import os,sys
from pathlib import Path
from gitsync.sync import SyncManager, SyncOptions
from gitsync.backends import FakeStorageBackend
from gitsync.storage_report import StorageVersion
import datetime as dt
b=FakeStorageBackend([StorageVersion(1,'Test',dt.datetime(2026,1,1),'v1')])
b.files={1:{'new.txt':'new'}}
m=SyncManager(Path(sys.argv[1]),b,SyncOptions(jobs=1,retries=0))
run=m.repo.run
def crash(args,**kwargs):
    result=run(args,**kwargs)
    if args[0]=='update-ref': os._exit(79)
    return result
m.repo.run=crash
m.sync()
'''
    child = subprocess.run([sys.executable, '-c', code, str(work)],
                           cwd=Path(__file__).resolve().parents[1] / 'src', capture_output=True)
    assert child.returncode == 79, child.stderr
    head = repo.head_sha()
    assert read_version_file(work) == 1
    result = _manager(work, _backend(1)).sync(raise_on_error=False)
    assert result.ok and result.committed == []
    assert repo.head_sha() == head
    assert repo.is_clean()
    assert not (work / '.git/gitsync-py-journal.json').exists()


def test_version_temporary_hardlink_never_truncates_external_inode(tmp_path):
    work = tmp_path / 'repo'
    _seed(work)
    outside = tmp_path / 'outside'
    outside.write_text('external')
    os.link(outside, work / 'VERSION.tmp')
    write_version_file(work, 7)
    assert outside.read_text() == 'external'
    assert (work / 'VERSION.tmp').read_text() == 'external'
    assert read_version_file(work) == 7


def test_journal_temporary_hardlink_never_truncates_external_inode(tmp_path):
    from gitsync.sync import _Journal

    work = tmp_path / 'repo'
    _seed(work)
    outside = tmp_path / 'outside'
    outside.write_text('external')
    path = work / '.git/gitsync-py-journal.json'
    os.link(outside, path.with_name(path.name + '.tmp'))
    _Journal(path).write({'test': 1})
    assert outside.read_text() == 'external'


def test_copy_helper_detaches_hardlink(tmp_path):
    from gitsync.sync import move_export_into_working_copy

    work = tmp_path / 'repo'
    _seed(work)
    outside = tmp_path / 'outside'
    outside.write_text('external')
    os.link(outside, work / '.gitignore')
    export = tmp_path / 'export'
    export.mkdir()
    (export / '.gitignore').write_text('snapshot')
    move_export_into_working_copy(work, export)
    assert outside.read_text() == 'external'
    assert (work / '.gitignore').read_text() == 'snapshot'


def test_commit_ack_survives_both_journal_write_and_read_failure(tmp_path, monkeypatch):
    import gitsync.sync as sm

    work = tmp_path / 'repo'
    repo = _seed(work)
    write, read = sm._Journal.write, sm._Journal.read
    committed = False

    def fail_write(self, payload):
        nonlocal committed
        if payload['state'] == 'committed':
            committed = True
            raise OSError('journal write unavailable')
        return write(self, payload)

    def fail_read(self):
        if committed:
            raise OSError('journal read unavailable')
        return read(self)

    monkeypatch.setattr(sm._Journal, 'write', fail_write)
    monkeypatch.setattr(sm._Journal, 'read', fail_read)
    result = _manager(work, _backend(1)).sync(raise_on_error=False)
    assert result.committed == [1] and result.post_commit
    assert read_version_file(work) == 1
    assert '<VERSION>1</VERSION>' in repo.run(['show', 'HEAD:VERSION']).stdout


def test_rollback_preserves_unrelated_empty_directories(tmp_path):
    work = tmp_path / 'repo'
    _seed(work)
    outsider = work / 'human-empty'
    outsider.mkdir()
    backend = _backend(1)
    backend.files = {1: {'owned-new/deep/file.txt': 'new'}}
    manager = _manager(work, backend)

    def fail(**kwargs):
        raise RuntimeError('precommit failure')

    manager.repo.commit_all = fail
    result = manager.sync(raise_on_error=False)
    assert result.error is not None
    assert outsider.is_dir()
    assert not (work / 'owned-new').exists()


def test_removed_directory_with_concurrent_outsider_is_not_recursively_deleted(tmp_path, monkeypatch):
    import gitsync.sync as sm

    work = tmp_path / 'repo'
    _seed(work, 0, {'src/old.txt': 'old'})
    original = sm.put_image
    outsider = work / 'src/human.txt'

    def inject(path, value):
        original(path, value)
        if path == work / 'src/old.txt' and value is None:
            outsider.write_bytes(b'human bytes')

    monkeypatch.setattr(sm, 'put_image', inject)
    result = _manager(work, _backend(1), disable_auto_src=True).sync(raise_on_error=False)
    assert result.ok
    assert outsider.read_bytes() == b'human bytes'
    assert not (work / 'src/old.txt').exists()


def test_external_owned_edit_during_export_is_preserved(tmp_path):
    work = tmp_path / 'repo'
    repo = _seed(work, 0, {'object.txt': 'old'})
    backend = _backend(1)
    original = backend.export_version

    def export(*args, **kwargs):
        original(*args, **kwargs)
        (work / 'object.txt').write_text('external editor')
        repo.run(['add', 'object.txt'])

    backend.export_version = export
    result = _manager(work, backend).sync(raise_on_error=False)
    assert result.error is not None
    assert (work / 'object.txt').read_text() == 'external editor'
    assert repo.run(['show', ':object.txt']).stdout == 'external editor'
    assert repo.run(['show', 'HEAD:object.txt']).stdout == 'old'
