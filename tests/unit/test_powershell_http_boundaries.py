from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_mcp_surface_powershell_requests_have_a_finite_timeout() -> None:
    source = (REPO_ROOT / "scripts" / "validate-bhm-mcp-surface.ps1").read_text(encoding="utf-8")
    assert "$BhmHttpTimeoutSec = 15" in source
    assert source.count("-TimeoutSec $BhmHttpTimeoutSec") >= 5


def test_mcp_surface_retry_helper_does_not_leave_unbounded_rest_calls() -> None:
    source = (REPO_ROOT / "scripts" / "validate-bhm-mcp-surface.ps1").read_text(encoding="utf-8")
    helper = source.split("function Invoke-BhmJsonRequest", 1)[1].split("function Invoke-BhmJson", 1)[0]
    assert helper.count("Invoke-RestMethod") == 2
    assert helper.count("-TimeoutSec $BhmHttpTimeoutSec") == 2
