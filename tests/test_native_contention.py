"""Regression for native07 same-login contention, at the actual Designer seam."""
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gitsync.backends import NativeStorageBackend
from gitsync.designer import DesignerRunner, StorageAccess
from gitsync.errors import DesignerError


def test_same_login_downloads_serialized_but_private_processing_overlaps(tmp_path):
    first_session = threading.Event()
    second_ready = threading.Event()
    release_session = threading.Event()
    conflict = threading.Event()
    processing = threading.Barrier(2)
    session = threading.Lock()
    calls = []

    class NativeSessionRunner(DesignerRunner):
        def run(self, args, timeout=None):
            if "/ConfigurationRepositoryDumpCfg" in args:
                if not session.acquire(blocking=False):
                    conflict.set()
                    raise DesignerError("Пользователь уже аутентифицирован в хранилище")
                try:
                    calls.append("download")
                    first_session.set()
                    assert release_session.wait(5)
                    Path(args[args.index("/ConfigurationRepositoryDumpCfg") + 1]).write_bytes(b"cf")
                finally:
                    session.release()
            elif "/LoadCfg" in args:
                # Both independent workers must enter processing before either can finish.
                processing.wait(5)
            elif "/DumpConfigToFiles" in args:
                Path(args[args.index("/DumpConfigToFiles") + 1], "Configuration.xml").write_text("xml")

    def backend(number):
        def create(root):
            if number == 2:
                second_ready.set()
            return "/F" + str(root / "ib")
        # Distinct backends and scratch roots, same canonical storage+login.
        storage = tmp_path / "repository" if number == 1 else tmp_path / "alias" / ".." / "repository"
        return NativeStorageBackend(
            StorageAccess(str(storage), "reader"), NativeSessionRunner("1cv8", tmp_path / str(number)),
            tmp_path / f"workers{number}", ib_factory=create,
        )

    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(backend(1).export_version, 1, tmp_path / "xml1")
        assert first_session.wait(5)
        two = pool.submit(backend(2).export_version, 2, tmp_path / "xml2")
        assert second_ready.wait(5)
        # First session stays occupied until the competing request has had a chance to arrive.
        conflict.wait(0.5)
        release_session.set()
        errors = [f.exception(timeout=10) for f in (one, two)]
    assert not conflict.is_set(), [str(e) for e in errors]
    assert errors == [None, None]
    assert calls == ["download", "download"]


def test_session_lock_shared_with_another_process_and_bounded(tmp_path):
    import os
    import subprocess
    import sys

    from gitsync.locks import exclusive_lock
    from gitsync.repository_session import repository_session_path

    access = StorageAccess(str(tmp_path / "repository"), "reader")
    script = """
import sys
from pathlib import Path
from gitsync.backends import NativeStorageBackend
from gitsync.designer import DesignerRunner, StorageAccess
from gitsync.errors import LockBusyError
class Runner(DesignerRunner):
    def run(self, args, timeout=None):
        raise AssertionError('Designer must not start while another process owns session')
b = NativeStorageBackend(StorageAccess(sys.argv[1], 'reader'),
    Runner('unused', Path(sys.argv[2]), timeout=.15), Path(sys.argv[2]),
    ib_factory=lambda p: '/Funused')
try:
    b.fetch_history()
except LockBusyError:
    print('SESSION_BUSY')
else:
    raise AssertionError('missing process lock')
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    with exclusive_lock(repository_session_path(access)):
        child = subprocess.run([sys.executable, "-c", script, access.path, str(tmp_path / "child")],
                               env=env, capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "SESSION_BUSY"


def test_unrelated_repository_or_user_not_serialized(tmp_path):
    from gitsync.repository_session import repository_session_path

    base = StorageAccess(str(tmp_path / "a"), "reader")
    assert repository_session_path(base) != repository_session_path(StorageAccess(base.path, "other"))
    other = StorageAccess(str(tmp_path / "b"), "reader")
    changed_password = StorageAccess(base.path, "READER", "secret")
    assert repository_session_path(base) != repository_session_path(other)
    assert repository_session_path(base) == repository_session_path(changed_password)
    assert "secret" not in str(repository_session_path(base))


def test_failed_auth_releases_session_without_retry(tmp_path):
    import pytest

    from gitsync.locks import exclusive_lock
    from gitsync.repository_session import repository_session_path

    calls = []
    class RejectAuth(DesignerRunner):
        def run(self, args, timeout=None):
            calls.append(args)
            raise DesignerError("invalid credentials")
    access = StorageAccess(str(tmp_path / "repository"), "reader")
    backend = NativeStorageBackend(access, RejectAuth("unused", tmp_path), tmp_path / "worker",
                                   ib_factory=lambda p: "/Fprivate")
    with pytest.raises(DesignerError, match="invalid credentials"):
        backend.export_version(1, tmp_path / "xml")
    assert len(calls) == 1
    with exclusive_lock(repository_session_path(access), timeout=0):
        pass
