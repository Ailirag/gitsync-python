"""Native extension command and scratch bootstrap regressions."""
from pathlib import Path

import pytest

from gitsync.backends import NativeStorageBackend
from gitsync.designer import DesignerRunner, StorageAccess
from gitsync.errors import DesignerError


class RecordingRunner(DesignerRunner):
    def __init__(self, root, fail_bootstrap=False):
        super().__init__("1cv8.exe", root)
        self.calls = []
        self.fail_bootstrap = fail_bootstrap

    def run(self, args, timeout=None):
        self.calls.append(args)
        if "/LoadCfg" in args and len(self.calls) == 1 and self.fail_bootstrap:
            raise DesignerError("bootstrap failed")
        if "/ConfigurationRepositoryReport" in args:
            Path(args[args.index("/ConfigurationRepositoryReport") + 1]).write_bytes(b"report")
        if "/ConfigurationRepositoryDumpCfg" in args:
            Path(args[args.index("/ConfigurationRepositoryDumpCfg") + 1]).write_bytes(b"cfe")
        if "/DumpConfigToFiles" in args:
            Path(args[args.index("/DumpConfigToFiles") + 1], "Configuration.xml").write_text("xml")


@pytest.mark.parametrize("operation", ["history", "export"])
def test_backend_bootstraps_before_read_and_propagates_extension(tmp_path, monkeypatch, operation):
    runner = RecordingRunner(tmp_path)
    backend = NativeStorageBackend(
        StorageAccess("repository", "reader"), runner, tmp_path / "workers",
        extension="UnrelatedName", ib_factory=lambda root: "/F" + str(root / "ib"),
    )
    monkeypatch.setattr("gitsync.backends.parse_storage_report", lambda data: [])
    if operation == "history":
        backend.fetch_history()
        backend.fetch_history(2)
        command = "/ConfigurationRepositoryReport"
    else:
        backend.export_version(1, tmp_path / "dump1")
        backend.export_version(2, tmp_path / "dump2")
        command = "/ConfigurationRepositoryDumpCfg"
    first = runner.calls[0]
    assert "/LoadCfg" in first, "extension must exist before repository access"
    seed = Path(first[first.index("/LoadCfg") + 1])
    assert seed.is_file() and seed.stat().st_size > 0
    assert first[-2:] == ["-Extension", "UnrelatedName"]
    assert sum(str(seed) in args for args in runner.calls) == 1
    for args in runner.calls:
        if command in args:
            assert args[-2:] == ["-Extension", "UnrelatedName"]


def test_bootstrap_failure_does_not_cache_context_or_access_repository(tmp_path):
    runner = RecordingRunner(tmp_path, fail_bootstrap=True)
    backend = NativeStorageBackend(
        StorageAccess("repository", "reader"), runner, tmp_path / "workers",
        extension="UnrelatedName", ib_factory=lambda root: "/F" + str(root / "ib"),
    )
    with pytest.raises(DesignerError, match="bootstrap failed"):
        backend._worker_context()
    assert getattr(backend._local, "context", None) is None
    assert not any("/ConfigurationRepositoryReport" in args for args in runner.calls)
    backend.cleanup()
    assert not list((tmp_path / "workers").iterdir())


@pytest.mark.parametrize("operation", ["report", "repository_dump"])
def test_repository_operation_preserves_extension(tmp_path, operation):
    runner = DesignerRunner("1cv8.exe", tmp_path)
    access = StorageAccess("repository", "reader")
    if operation == "report":
        args = runner.build_report_args(access, tmp_path / "history.mxl", extension="OtherExtension")
    else:
        args = runner.build_dump_cfg_args(
            access, 2, tmp_path / "version.cfe", "/Fscratch", extension="OtherExtension"
        )
    assert args[-2:] == ["-Extension", "OtherExtension"]
