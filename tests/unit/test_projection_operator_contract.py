from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATOR = REPO_ROOT / "scripts" / "bhm-projection-operator.ps1"


def test_projection_operator_contract_is_bounded_and_fail_closed():
    text = OPERATOR.read_text(encoding="utf-8")

    for marker in (
        'ValidateSet("status", "dry-run", "drain")',
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
    ):
        assert marker in text


def test_projection_operator_does_not_enable_unbounded_worker_loop():
    text = OPERATOR.read_text(encoding="utf-8")
    assert "--loop" in text
    assert "--max-cycles $MaxCycles" in text
    assert "--loop --force" not in text
