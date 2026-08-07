from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATOR = REPO_ROOT / "scripts" / "bhm-projection-operator.ps1"


def test_projection_operator_contract_is_bounded_and_fail_closed():
    text = OPERATOR.read_text(encoding="utf-8")

    for marker in (
        'ValidateSet("status", "dry-run", "drain")',
        "Add-Type -AssemblyName System.Net.Http",
        "MaxCycles",
        "sqlite-authoritative",
        "sqlite-shadow",
        "run-bhm-projection-worker.py",
        "start-bhm-authoritative.ps1",
        "SemanticFusion",
        "projection_pending",
        "projection_failed",
        "requires a ready sqlite-authoritative runtime",
        "dead_letter",
        "Get-LiveSloWithRetry",
        "BHM SLO unavailable after",
        "-MaxAttempts 120 -DelaySeconds 1",
        "Get-ConfiguredOpenAiBaseUrl",
        "Test-OpenAiBaseUrl",
        "Get-BhmRuntimeEndpoint -Name 'llm_default'",
        "staleDockerHost",
        "Assert-ProjectionOperatorUri",
        "Invoke-ProjectionOperatorJson",
        "ProjectionHttpTimeoutSec = 10",
        "ProjectionHttpMaxResponseBytes = 262144",
        "AllowAutoRedirect = $false",
        "UseProxy = $false",
        "ResponseHeadersRead",
        "must not contain userinfo",
        "must not contain query or fragment",
    ):
        assert marker in text
    assert "Invoke-WebRequest" not in text
    assert "Invoke-RestMethod" not in text


def test_projection_operator_does_not_enable_unbounded_worker_loop():
    text = OPERATOR.read_text(encoding="utf-8")
    assert "--loop" in text
    assert "--max-cycles $MaxCycles" in text
    assert "--loop --force" not in text


def test_projection_operator_rejects_non_loopback_probe_before_network():
    script = OPERATOR
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Action",
            "status",
            "-BaseUrl",
            "https://example.com",
            "-AsJson",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "BHM_CALLER_TOKEN": "x" * 40},
        check=False,
    )
    assert completed.returncode != 0
    assert "loopback endpoint" in (completed.stdout + completed.stderr).lower()
