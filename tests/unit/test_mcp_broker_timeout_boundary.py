from __future__ import annotations

from pathlib import Path

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
    assert "open(self.pipe_path" not in text
    assert "join(timeout=3.0)" not in text
    assert "wait(timeout=0.2)" not in text


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
