"""Opt-in Python remote policy; only this test's own local bare repository."""
import pytest

from gitsync.backends import FixtureStorageBackend
from gitsync.cli import main
from gitsync.gitrepo import GitRepo
from gitsync.sync import SyncManager
from test_cli import _git
from test_cli import fixture_storage as _fixture_storage
from test_parity07_clone import seed_remote
from test_parity07_plugins import load_plugin

fixture_storage = _fixture_storage


@pytest.mark.parametrize('outcome', ['success', 'reject', 'diverge'])
def test_explicit_local_remote_policy(tmp_path, fixture_storage, monkeypatch, outcome):
    remote, initial = seed_remote(tmp_path, markers=True)
    work = tmp_path / 'clone'
    assert main(['clone', '--url', str(remote), '--workdir', str(work),
                 '--backend', 'fixture', '--fixture-root', str(fixture_storage)]) == 0
    # Independent other writer advances only the test-owned remote.
    seed = tmp_path / 'seed'
    (seed / 'README').write_text('new upstream README')
    remote_head = GitRepo(seed).commit_all('remote advance', 'Remote <remote@example.org>')
    _git(seed, 'push', str(remote), 'HEAD:refs/heads/main')
    if outcome == 'diverge':
        (work / 'sibling').write_text('independent local history')
        local_head = GitRepo(work).commit_all('local advance', 'Local <local@example.org>')
    if outcome == 'reject':
        hook = remote / 'hooks' / 'pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        hook.chmod(0o755)
    host = load_plugin(tmp_path, monkeypatch, f'''
from gitsync.gitrepo import GitRepo
REMOTE = {str(remote)!r}
def register(host):
    def pull(ctx):
        repo = GitRepo(ctx.work_dir)
        repo.ensure_clean()
        repo.run(['fetch', '--no-tags', '--', REMOTE, 'refs/heads/main'])
        repo.run(['merge', '--ff-only', 'FETCH_HEAD'])
    def push(ctx):
        GitRepo(ctx.work_dir).run(['push', '--', REMOTE, 'HEAD:refs/heads/main'])
    host.subscribe('before_sync', pull, contextual=True)
    host.subscribe('after_commit', push, contextual=True)
''')
    manager = SyncManager(work, FixtureStorageBackend(fixture_storage), plugins=host)
    result = manager.sync(raise_on_error=False)
    if outcome == 'diverge':
        assert not result.ok
        assert result.committed == []
        assert GitRepo(work).head_sha() == local_head
        assert _git(remote, 'rev-parse', 'HEAD').strip() == remote_head
    else:
        assert result.committed == [2]
        assert _git(work, 'rev-parse', 'HEAD~1').strip() == remote_head
        assert (work / 'README').read_text() == 'new upstream README'
        assert '<VERSION>2</VERSION>' in (work / 'src' / 'VERSION').read_text()
        if outcome == 'reject':
            assert not result.ok and result.post_commit
            assert _git(remote, 'rev-parse', 'HEAD').strip() == remote_head
            # Explicit retry policy/operator action; no reset of durable local commit.
            hook.unlink()
            _git(work, 'push', str(remote), 'HEAD:refs/heads/main')
        else:
            assert result.ok
        assert _git(remote, 'rev-parse', 'HEAD').strip() == GitRepo(work).head_sha()
    assert initial != remote_head
