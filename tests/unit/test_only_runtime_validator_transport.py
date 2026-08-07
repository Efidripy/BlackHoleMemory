from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-only-runtime.ps1"


def test_only_runtime_validator_uses_bounded_loopback_transport():
    text = SCRIPT.read_text(encoding="utf-8")

    for marker in (
        "Add-Type -AssemblyName System.Net.Http",
        "Assert-RuntimeValidatorUri",
        "Invoke-RuntimeValidatorJson",
        "RuntimeValidatorHttpTimeoutSec = 20",
        "RuntimeValidatorMaxResponseBytes = 262144",
        "AllowAutoRedirect = $false",
        "UseProxy = $false",
        "ResponseHeadersRead",
        "must not contain userinfo",
        "must not contain a query",
        "must not contain a fragment",
        "requires an HTTP loopback endpoint",
    ):
        assert marker in text

    assert "Invoke-RestMethod" not in text
