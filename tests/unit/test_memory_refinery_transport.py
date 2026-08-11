from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run-memory-refinery.ps1"


def test_memory_refinery_uses_bounded_loopback_transport():
    text = SCRIPT.read_text(encoding="utf-8")

    for marker in (
        "Assert-RefineryUri",
        "Invoke-RefineryJson",
        "RefineryHttpTimeoutSec = 20",
        "RefineryHttpMaxResponseBytes = 262144",
        "AllowAutoRedirect = $false",
        "UseProxy = $false",
        "ResponseHeadersRead",
        "must not contain userinfo",
        "must not contain a fragment",
        "requires an HTTP loopback endpoint",
        "ApplySafe",
        "PageSize = 500",
        "$script:refineryProjects",
        'if ($ApplySafe) {',
        "aggregate = $false",
    ):
        assert marker in text

    assert "Invoke-RestMethod" not in text
    assert 'limit=1000&offset=0' not in text
