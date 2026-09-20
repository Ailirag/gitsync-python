"""R07 acceptance invariants: real NTFS junctions and owned resource lifecycle."""
import os
import stat
import subprocess

import pytest

from gitsync.backends import FixtureStorageBackend
from gitsync.errors import UnsafePathError
from gitsync.gitrepo import GitRepo
from gitsync.plugins import PluginHost
from gitsync.sync import SyncManager, SyncOptions
from gitsync.version_file import read_version_file
from test_cli import fixture_storage as _fixture_storage

fixture_storage = _fixture_storage


@pytest.mark.parametrize('cleanup_failure', ['none', 'run', 'backend', 'both'])
def test_after_sync_failure_cleans_owned_resources(tmp_path, fixture_storage, monkeypatch,
                                                 cleanup_failure):
    from pathlib import Path

    import gitsync.sync as sync_module

    # Шов сдвинут на снятие каталога выгрузки в самом менеджере: fix18 удаляет каталог не
    # по пути (`shutil.rmtree`), а по удостоверению созданного объекта, и внутрь уходят
    # уже имена вложенных каталогов. Проверяемое поведение то же: одна попытка снятия
    # после остановки пула, отказ попадает в результат, чужое в общем родителе цело.
    original_discard = sync_module.discard_owned_directory
    attempts = []

    def remove(path, *args, **kwargs):
        if Path(path).name.startswith('run-'):
            attempts.append(path)
            if cleanup_failure in ('run', 'both'):
                raise OSError('run cleanup failure')
        return original_discard(path, *args, **kwargs)

    monkeypatch.setattr(sync_module, 'discard_owned_directory', remove)
    shared = tmp_path / 'shared'
    (shared / 'foreign' / 'empty').mkdir(parents=True)
    (shared / 'foreign' / 'keep').write_bytes(b'FOREIGN')
    foreign = tree_bytes(shared / 'foreign')
    resource = shared / 'backend-owned'

    class Backend(FixtureStorageBackend):
        cleaned = 0
        worker = None

        def export_version(self, version, dest, cancel=None):
            import threading
            self.worker = threading.current_thread()
            resource.mkdir(exist_ok=True)
            (resource / 'data').write_bytes(b'OWNED')
            return super().export_version(version, dest, cancel)

        def cleanup(self):
            import shutil
            assert self.worker is None or not self.worker.is_alive()
            self.cleaned += 1
            shutil.rmtree(resource)
            if cleanup_failure in ('backend', 'both'):
                raise OSError('backend cleanup failure')

    backend = Backend(fixture_storage)
    host = PluginHost()
    original = RuntimeError('after_sync original failure')

    def fail(ctx):
        raise original

    host.subscribe('after_sync', fail, contextual=True)
    work = tmp_path / 'work'
    repo = GitRepo(work)
    repo.init()
    (work / 'VERSION').write_text('<VERSION>0</VERSION>')
    (work / 'original').write_bytes(b'PREIMAGE')
    seed = repo.commit_all('seed', 'Seed <seed@example.org>')
    manager = SyncManager(work, backend, SyncOptions(limit=1, temp_root=shared), host)
    result = manager.sync(raise_on_error=False)
    assert backend.cleaned == 1
    assert len(attempts) == 1
    assert not resource.exists()
    assert bool(list(shared.glob('run-*'))) == (cleanup_failure in ('run', 'both'))
    assert tree_bytes(shared / 'foreign') == foreign
    assert result.error is original or result.error.__cause__ is original
    if cleanup_failure != 'none':
        notes = '\n'.join(getattr(original, '__notes__', []))
        for kind in ('run', 'backend') if cleanup_failure == 'both' else [cleanup_failure]:
            assert kind + ' cleanup failure' in notes
    assert result.post_commit and result.committed == [1]
    assert read_version_file(work) == 1 and repo.head_sha() != seed
    assert repo.is_clean() and manager._journal().read() is None
    assert not (work / '.git' / 'index.lock').exists()
    from gitsync.locks import exclusive_lock
    with exclusive_lock(work / '.git' / 'gitsync-py.lock', timeout=0):
        pass
    durable_head = repo.head_sha()
    monkeypatch.setattr(sync_module, 'discard_owned_directory', original_discard)
    recovery = SyncManager(work, FixtureStorageBackend(fixture_storage),
                           SyncOptions(limit=1, temp_root=shared)).sync()
    assert recovery.committed == [2]
    assert repo.run(['rev-parse', 'HEAD~1']).stdout.strip() == durable_head


@pytest.mark.parametrize('keep_temp', [False, True])
@pytest.mark.parametrize('noop', [False, True])
def test_after_sync_error_keep_temp_and_noop_contract(tmp_path, fixture_storage, keep_temp, noop):
    shared = tmp_path / 'shared'
    shared.mkdir()
    (shared / 'foreign').write_bytes(b'KEEP')
    resource = shared / 'backend-resource'

    class Backend(FixtureStorageBackend):
        cleaned = 0

        def fetch_history(self, begin=1):
            resource.write_bytes(b'OWNED')
            return super().fetch_history(begin)

        def cleanup(self):
            self.cleaned += 1
            resource.unlink()

    backend = Backend(fixture_storage)
    host = PluginHost()
    original = RuntimeError('notification failed')

    def fail(ctx):
        raise original

    host.subscribe('after_sync', fail, contextual=True)
    work = tmp_path / 'work'
    repo = GitRepo(work)
    repo.init()
    (work / 'VERSION').write_text('<VERSION>2</VERSION>' if noop else '<VERSION>0</VERSION>')
    seed = repo.commit_all('seed', 'Seed <seed@example.org>')
    manager = SyncManager(work, backend,
                          SyncOptions(limit=1, temp_root=shared, cleanup_temp=not keep_temp), host)
    with pytest.raises(RuntimeError) as caught:
        manager.sync()
    assert caught.value is original
    assert backend.cleaned == (0 if keep_temp else 1)
    assert resource.exists() == keep_temp
    assert bool(list(shared.glob('run-*'))) == (keep_temp and not noop)
    assert (shared / 'foreign').read_bytes() == b'KEEP'
    assert read_version_file(work) == (2 if noop else 1)
    assert (repo.head_sha() == seed) == noop
    assert repo.is_clean() and manager._journal().read() is None


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): None if p.is_dir() else p.read_bytes()
            for p in root.rglob('*')}


@pytest.mark.skipif(os.name != 'nt', reason='Actual NTFS junction regression')
@pytest.mark.parametrize('parent', [False, True], ids=['target', 'ancestor'])
def test_clone_rejects_actual_junction_before_any_write(tmp_path, monkeypatch, parent):
    seed = tmp_path / 'seed'
    repo = GitRepo(seed)
    repo.init()
    (seed / 'data').write_bytes(b'CLONE')
    repo.commit_all('seed', 'Seed <seed@example.org>')
    external = tmp_path / 'external'
    external.mkdir()
    if parent:
        (external / 'existing' / 'empty').mkdir(parents=True)
        (external / 'existing' / 'precious').write_bytes(b'UNCHANGED\x00\xff')
    link = tmp_path / 'link'
    subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(external)],
                   check=True, capture_output=True)
    try:
        info = link.lstat()
        assert info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        assert info.st_reparse_tag == 0xA0000003
        before = tree_bytes(external)
        calls = []
        original = subprocess.run

        def record(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        monkeypatch.setattr(subprocess, 'run', record)
        target = link / 'missing' / 'child' if parent else link
        error = None
        try:
            GitRepo(target).clone(str(seed))
        except UnsafePathError as exc:
            error = exc
        assert tree_bytes(external) == before, 'clone modified the external tree'
        assert not calls, 'Git must not run before destination containment validation'
        assert error is not None, 'junction target/ancestors must be rejected'
    finally:
        os.rmdir(link)  # unlink only our junction, never traverse the external tree
