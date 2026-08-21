from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.filesystem_boundaries import FilesystemBoundaryError
from blackholememory.resource_limits import MCP_BROKER_CAPACITY_WAIT_SECONDS
from blackholememory.resource_limits import MCP_BROKER_JOIN_TIMEOUT_SECONDS
from blackholememory.resource_limits import MCP_BROKER_WAKE_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]


def test_mcp_broker_lifecycle_timeouts_are_registry_backed() -> None:
    assert MCP_BROKER_JOIN_TIMEOUT_SECONDS == 3.0
    assert MCP_BROKER_CAPACITY_WAIT_SECONDS == 0.2
    assert MCP_BROKER_WAKE_TIMEOUT_SECONDS == 0.2
    text = (ROOT / "src" / "blackholememory" / "infra" / "mcp_broker.py").read_text(encoding="utf-8")
    assert "MCP_BROKER_JOIN_TIMEOUT_SECONDS" in text
    assert "MCP_BROKER_CAPACITY_WAIT_SECONDS" in text
    assert "MCP_BROKER_WAKE_TIMEOUT_SECONDS" in text
    assert "wake_named_pipe" in text
    assert "client.settimeout(MCP_BROKER_WAKE_TIMEOUT_SECONDS)" in text
    assert "open(self.pipe_path" not in text
    assert "join(timeout=3.0)" not in text
    assert "wait(timeout=0.2)" not in text
    assert "client.settimeout(0.2)" not in text


def test_windows_named_pipe_wake_uses_bounded_probe_and_closes_handle() -> None:
    from blackholememory.infra.mcp_broker import _WindowsKernel32

    calls: dict[str, object] = {}

    class _FakeKernel32:
        def WaitNamedPipeW(self, path, timeout):
            calls["wait_path"] = path.value
            calls["wait_timeout_ms"] = timeout.value
            return 1

        def CreateFileW(self, path, access, share, security, creation, flags, template):
            calls["create_path"] = path.value
            calls["access"] = access.value
            calls["creation"] = creation.value
            return 42

        def CloseHandle(self, handle):
            calls["closed"] = handle.value
            return 1

    kernel32 = object.__new__(_WindowsKernel32)
    kernel32._kernel32 = _FakeKernel32()

    assert kernel32.wake_named_pipe(r"\\.\pipe\bhm", timeout_seconds=0.2) is True
    assert calls == {
        "wait_path": r"\\.\pipe\bhm",
        "wait_timeout_ms": 200,
        "create_path": r"\\.\pipe\bhm",
        "access": 0xC0000000,
        "creation": 3,
        "closed": 42,
    }


def test_single_instance_lock_rejects_hardlinked_target_before_open(tmp_path, monkeypatch) -> None:
    from blackholememory.infra import mcp_broker

    monkeypatch.setattr(mcp_broker.tempfile, "gettempdir", lambda: str(tmp_path))
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"do-not-touch")
    target = tmp_path / "broker.lock"
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError, match="hardlink"):
        mcp_broker._SingleInstanceLock("broker").acquire()
    assert outside.read_bytes() == b"do-not-touch"


def test_single_instance_lock_rejects_reparse_parent_before_creation(tmp_path, monkeypatch) -> None:
    from blackholememory.infra import mcp_broker

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    monkeypatch.setattr(mcp_broker.tempfile, "gettempdir", lambda: str(linked_parent))

    with pytest.raises(FilesystemBoundaryError, match="symlink|reparse"):
        mcp_broker._SingleInstanceLock("broker").acquire()
    assert not (outside / "broker.lock").exists()


def test_unix_socket_cleanup_rejects_non_socket_path(tmp_path) -> None:
    from blackholememory.infra import mcp_broker

    target = tmp_path / "broker.sock"
    target.write_text("do-not-delete", encoding="utf-8")

    with pytest.raises(FilesystemBoundaryError, match="not a Unix socket"):
        mcp_broker.McpIpcBroker._remove_unix_socket(target)
    assert target.read_text(encoding="utf-8") == "do-not-delete"
