"""Real local Git only: clone prepares metadata; explicit sync continues history."""
from gitsync.cli import main
from gitsync.gitrepo import GitRepo
from test_cli import _git
from test_cli import fixture_storage as _fixture_storage

fixture_storage = _fixture_storage


def seed_remote(tmp_path, *, markers=False):
    seed = tmp_path / 'seed'
    repo = GitRepo(seed)
    repo.init()
    (seed / 'README').write_text('keep root', encoding='utf-8')
    (seed / 'src').mkdir()
    (seed / 'src' / 'old.xml').write_text('old', encoding='utf-8')
    if markers:
        (seed / 'src' / 'VERSION').write_text('<VERSION>1</VERSION>\n', encoding='utf-8')
        (seed / 'src' / 'AUTHORS').write_text('Петров=Custom <custom@example.org>\n', encoding='utf-8')
    repo.commit_all('seed', 'Seed <seed@example.org>')
    remote = tmp_path / 'own-local.git'
    _git(tmp_path, 'clone', '--bare', str(seed), str(remote))
    return remote, repo.head_sha()


def test_clone_real_remote_prepares_src_then_sync_continues(tmp_path, fixture_storage):
    remote, head = seed_remote(tmp_path)
    work = tmp_path / 'clone'
    args = ['--workdir', str(work), '--backend', 'fixture', '--fixture-root', str(fixture_storage)]
    assert main(['clone', '--url', str(remote), *args]) == 0
    assert _git(work, 'rev-parse', 'HEAD').strip() == head
    assert _git(work, 'remote', 'get-url', 'origin').strip().replace('\\', '/') == remote.as_posix()
    assert '<VERSION>0</VERSION>' in (work / 'src' / 'VERSION').read_text()
    assert (work / 'src' / 'AUTHORS').is_file()
    assert not (work / 'VERSION').exists()
    assert main(['sync', *args]) == 0
    assert _git(work, 'rev-list', '--count', 'HEAD').strip() == '3'
    assert (work / 'README').read_text() == 'keep root'
    assert not (work / 'src' / 'old.xml').exists()
    assert main(['sync', *args]) == 0
    assert _git(work, 'rev-list', '--count', 'HEAD').strip() == '3'
    assert _git(remote, 'rev-parse', 'HEAD').strip() == head  # no implicit push


def test_clone_refuses_nonempty_destination_before_git(tmp_path, fixture_storage, capsys):
    work = tmp_path / 'occupied'
    work.mkdir()
    (work / 'precious').write_bytes(b'unchanged')
    args = ['--workdir', str(work), '--backend', 'fixture', '--fixture-root', str(fixture_storage)]
    assert main(['clone', '--url', str(tmp_path / 'absent.git'), *args]) != 0
    assert 'nonempty destination' in capsys.readouterr().err
    assert list(work.iterdir()) == [work / 'precious']
    assert (work / 'precious').read_bytes() == b'unchanged'


def test_clone_file_url_preserves_custom_metadata_and_init_resume(tmp_path, fixture_storage):
    remote, head = seed_remote(tmp_path, markers=True)
    work = tmp_path / 'clone'
    args = ['--workdir', str(work), '--backend', 'fixture', '--fixture-root', str(fixture_storage)]
    assert main(['clone', '--url', remote.as_uri(), *args]) == 0
    authors = (work / 'src' / 'AUTHORS').read_bytes()
    marker = (work / 'src' / 'VERSION').read_bytes()
    assert main(['init', *args]) == 0
    assert (work / 'src' / 'AUTHORS').read_bytes() == authors
    assert (work / 'src' / 'VERSION').read_bytes() == marker
    assert main(['sync', *args]) == 0
    assert _git(work, 'rev-parse', 'HEAD~1').strip() == head
    assert _git(work, 'log', '-1', '--format=%an|%ae').strip() == 'Custom|custom@example.org'
    assert (work / 'src' / 'AUTHORS').read_bytes() == authors
