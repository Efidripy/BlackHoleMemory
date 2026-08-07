from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.tools import infra_healer


def _make_hardlink(target: Path, source: Path) -> None:
    source.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")


def test_mcp_reset_marker_rejects_hardlink_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "mcp-bridge-reset.json"
    outside = tmp_path / "outside.json"
    _make_hardlink(target, outside)
    monkeypatch.setenv(infra_healer.MCP_RESET_MARKER_ENV, str(target))

    with pytest.raises(OSError, match="hardlink"):
        infra_healer._write_mcp_reset_marker()
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_mcp_reset_marker_writes_json_with_boundary_safe_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "nested" / "mcp-bridge-reset.json"
    monkeypatch.setenv(infra_healer.MCP_RESET_MARKER_ENV, str(target))
    monkeypatch.setenv(infra_healer.MCP_PROCESS_RESET_ENV, "true")

    result = infra_healer._write_mcp_reset_marker()
    assert result == target
    payload = __import__("json").loads(target.read_text(encoding="utf-8"))
    assert payload["pid"] > 0
    assert payload["process_reset_enabled"] is True
