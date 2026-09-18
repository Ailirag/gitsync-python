"""Python hook contract exercised through real module imports and Git commits."""
import pytest

from gitsync.backends import FixtureStorageBackend
from gitsync.plugins import PluginHost
from gitsync.sync import SyncManager, SyncOptions
from test_cli import _git
from test_cli import fixture_storage as _fixture_storage

fixture_storage = _fixture_storage


def load_plugin(tmp_path, monkeypatch, code):
    monkeypatch.syspath_prepend(str(tmp_path))
    # Every test imports a distinct real module, never a fabricated callback result.
    name = 'plugin_' + tmp_path.name.replace('-', '_')
    (tmp_path / (name + '.py')).write_text(code, encoding='utf-8')
    host = PluginHost()
    host.load_module(name)
    return host


def test_mutable_commit_context_descending_priority(tmp_path, fixture_storage, monkeypatch):
    host = load_plugin(tmp_path, monkeypatch, '''
def register(host):
    def low(ctx):
        assert ctx.message == 'changed'
        ctx.message += ' then low'
        ctx.author = 'Plugin <plugin@example.org>'
    def high(ctx):
        ctx.message = 'changed'
    host.subscribe('before_commit', low, priority=-1, contextual=True)
    host.subscribe('before_commit', high, priority=20, contextual=True)
''')
    work = tmp_path / 'work'
    result = SyncManager(work, FixtureStorageBackend(fixture_storage),
                         SyncOptions(limit=1), host).sync()
    assert result.committed == [1]
    assert _git(work, 'log', '-1', '--format=%s|%an|%ae').strip() == (
        'changed then low|Plugin|plugin@example.org')


def test_history_override_replaces_backend_and_after_history_mutation(tmp_path, fixture_storage, monkeypatch):
    host = load_plugin(tmp_path, monkeypatch, '''
from datetime import datetime
from dataclasses import replace
from gitsync.storage_report import StorageVersion

def register(host):
    def history(ctx):
        ctx.standard_processing = False
        ctx.history = [StorageVersion(2, 'User', datetime(2026, 9, 15), 'override')]
    def after(ctx):
        ctx.history = [replace(v, comment=v.comment + ' edited') for v in ctx.history]
    host.subscribe('before_history', history, contextual=True)
    host.subscribe('after_history', after, contextual=True)
''')
    class NoHistory(FixtureStorageBackend):
        def fetch_history(self, start=1):
            raise AssertionError('standard history must not run')

    work = tmp_path / 'work'
    result = SyncManager(work, NoHistory(fixture_storage), plugins=host).sync()
    assert result.committed == [2]
    assert _git(work, 'log', '-1', '--format=%s').strip() == 'override edited'


def test_export_override_lifecycle_and_cleanup_preservation(tmp_path, fixture_storage, monkeypatch):
    host = load_plugin(tmp_path, monkeypatch, '''
events = []
def register(host):
    def export(ctx):
        ctx.standard_processing = False
        ctx.destination.mkdir()
        (ctx.destination / 'custom.xml').write_text('custom', encoding='utf-8')
    def cleanup(ctx):
        ctx.standard_processing = False
    def record(event):
        def handle(ctx):
            events.append(event)
        return handle
    host.subscribe('before_export', export, contextual=True)
    host.subscribe('before_cleanup', cleanup, contextual=True)
    for event in ['before_sync', 'before_history', 'after_history', 'before_export',
                  'after_export', 'before_cleanup', 'before_commit', 'after_commit', 'after_sync']:
        host.subscribe(event, record(event), priority=-10, contextual=True)
''')
    class NoExport(FixtureStorageBackend):
        def export_version(self, *args):
            raise AssertionError('standard export must not run')

    work = tmp_path / 'work'
    from gitsync.gitrepo import GitRepo
    repo = GitRepo(work)
    repo.init()
    (work / 'keep.xml').write_text('keep')
    (work / 'VERSION').write_text('<VERSION>0</VERSION>')
    repo.commit_all('seed', 'Seed <seed@example.org>')
    result = SyncManager(work, NoExport(fixture_storage), SyncOptions(limit=1), host).sync()
    assert result.committed == [1]
    assert _git(work, 'show', 'HEAD:keep.xml') == 'keep'
    assert _git(work, 'show', 'HEAD:custom.xml') == 'custom'
    import importlib
    assert importlib.import_module(host.names[0]).events == [
        'before_sync', 'before_history', 'after_history', 'before_export', 'after_export',
        'before_cleanup', 'before_commit', 'after_commit', 'after_sync']


@pytest.mark.parametrize('path', ['.git/config', 'nested/VERSION', 'nested/AUTHORS', 'nested/.GIT/config'])
def test_override_cannot_bypass_reserved_paths(tmp_path, fixture_storage, monkeypatch, path):
    host = load_plugin(tmp_path, monkeypatch, f'''
def register(host):
    def export(ctx):
        ctx.standard_processing = False
        target = ctx.destination / {path!r}
        target.parent.mkdir(parents=True)
        target.write_text('forbidden')
    host.subscribe('before_export', export, contextual=True)
''')
    work = tmp_path / 'work'
    manager = SyncManager(work, FixtureStorageBackend(fixture_storage), SyncOptions(limit=1), host)
    result = manager.sync(raise_on_error=False)
    assert not result.ok
    assert manager.repo.head_sha() is None
    assert not (work / 'VERSION').exists()


def test_cleanup_callback_cannot_rebaseline_external_write(tmp_path, fixture_storage, monkeypatch):
    host = load_plugin(tmp_path, monkeypatch, '''
def register(host):
    def cleanup(ctx):
        (ctx.work_dir / 'object.xml').write_text('external edit')
    host.subscribe('before_cleanup', cleanup, contextual=True)
''')
    from gitsync.gitrepo import GitRepo
    work = tmp_path / 'work'
    repo = GitRepo(work)
    repo.init()
    (work / 'object.xml').write_text('original')
    (work / 'VERSION').write_text('<VERSION>0</VERSION>')
    head = repo.commit_all('seed', 'Seed <seed@example.org>')
    result = SyncManager(work, FixtureStorageBackend(fixture_storage), SyncOptions(limit=1), host).sync(
        raise_on_error=False)
    assert not result.ok
    assert repo.head_sha() == head
    assert (work / 'object.xml').read_text() == 'external edit'


def test_legacy_callback_errors_fail_closed_by_default(tmp_path, fixture_storage):
    host = PluginHost()
    def fail(**kwargs):
        raise RuntimeError('plugin failed')
    host.subscribe('before_commit', fail)
    work = tmp_path / 'work'
    manager = SyncManager(work, FixtureStorageBackend(fixture_storage), SyncOptions(limit=1), host)
    result = manager.sync(raise_on_error=False)
    assert not result.ok
    assert manager.repo.head_sha() is None


def test_duplicate_history_from_hook_rejected_before_export(tmp_path, fixture_storage, monkeypatch):
    host = load_plugin(tmp_path, monkeypatch, """
def register(host):
    def duplicate(ctx):
        ctx.history = [ctx.history[0], ctx.history[0]]
    host.subscribe('after_history', duplicate, contextual=True)
""")
    work = tmp_path / 'work'
    manager = SyncManager(work, FixtureStorageBackend(fixture_storage), plugins=host)
    result = manager.sync(raise_on_error=False)
    assert not result.ok
    assert manager.repo.head_sha() is None
