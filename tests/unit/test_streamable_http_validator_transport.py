from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-streamable-http.ps1"


def test_streamable_http_validator_uses_bounded_loopback_transport():
    text = SCRIPT.read_text(encoding="utf-8")

    for marker in (
        "Assert-StreamableValidatorUri",
        "Invoke-StreamableValidatorJson",
        "StreamableHttpTimeoutSec = 10",
        "StreamableHttpMaxResponseBytes = 262144",
        "AllowAutoRedirect = $false",
        "UseProxy = $false",
        "ResponseHeadersRead",
        "must not contain userinfo",
        "must not contain a fragment",
        "requires an HTTP loopback endpoint",
    ):
        assert marker in text

    assert "Invoke-RestMethod" not in text
