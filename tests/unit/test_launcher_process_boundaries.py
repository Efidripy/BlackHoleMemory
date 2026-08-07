from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from blackholememory.resource_limits import PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS

import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import bhm_launcher as launcher  # noqa: E402


def test_install_worker_wait_uses_registry_bound(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 123
        stdout = ["ok"]

        def wait(self, *, timeout):
            captured["timeout"] = timeout
            return 0

    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    worker = launcher.InstallWorker.__new__(launcher.InstallWorker)
    worker.state_root = tmp_path
    worker.log_signal = SimpleNamespace(emit=lambda *_args: None)

    worker.run_command(["synthetic", "setup"])

    assert captured["timeout"] == PROCESS_EXECUTION_LAUNCHER_INSTALL_TIMEOUT_SECONDS


def test_install_worker_bounds_blocking_stdout_and_cleans_up(monkeypatch, tmp_path) -> None:
    released = threading.Event()
    cleanup: list[int] = []

    class BlockingStdout:
        def __iter__(self):
            released.wait()
            return iter(())

    class FakeProcess:
        pid = 456
        stdout = BlockingStdout()

        def wait(self, *, timeout):
            raise AssertionError("wait must not run after stdout deadline")

    def fake_cleanup(process):
        cleanup.append(process.pid)
        released.set()

    monkeypatch.setattr(launcher, "LAUNCHER_INSTALL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(launcher, "terminate_process_tree", fake_cleanup)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    worker = launcher.InstallWorker.__new__(launcher.InstallWorker)
    worker.state_root = tmp_path
    worker.log_signal = SimpleNamespace(emit=lambda *_args: None)

    with pytest.raises(RuntimeError, match="timed out"):
        worker.run_command(["synthetic", "hang"])

    assert cleanup == [456]


def test_release_doctor_uses_registry_process_timeout(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(launcher, "release_operator_path", lambda: tmp_path / "doctor.ps1")
    monkeypatch.setattr(launcher, "_assert_launchable_source", lambda *_args: tmp_path / "doctor.ps1")

    def fake_run(*_args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return SimpleNamespace(stdout="{}", stderr="")

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    assert launcher.run_release_doctor() == {}
    assert captured["timeout"] == PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS
