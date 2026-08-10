from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "scripts" / "start-bhm-authoritative.ps1"
SERVICE = REPO_ROOT / "scripts" / "run-service.ps1"


def test_authoritative_launcher_contract_is_fail_closed_and_explicit():
    text = LAUNCHER.read_text(encoding="utf-8")

    for marker in (
        'BHM_MEMORY_STORE_MODE = "sqlite-authoritative"',
        'BHM_FALLBACK_MODE = "explicit"',
        'BHM_PROJECTION_WORKER_ENABLED = "false"',
        'BHM_MEMORY_STORE_PARITY_CONFIRMED = "true"',
        'BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED = "true"',
        "/health/ready",
        "/bhm/health",
        "/health/cutover",
        "projection-only",
        "direct_vector_writes",
        "Wait-Authoritative",
        "ProbeOnly",
        "ForceRestart",
        "SemanticFusion",
        "-SemanticFusion",
        "BaseUrl",
        "Stop-BhmProcesses",
        "run-service.ps1",
        "'-Authoritative'",
        "start-qdrant.ps1",
        "rolled-back",
        "Resolve-LocalLmStudioEndpoint",
        "Assert-BhmApiLoopbackHost",
        "Test-OpenAiBaseUrl",
        "Get-BhmRuntimeEndpoint -Name 'lm_studio'",
        "Get-BhmRuntimeEndpointParts -Name 'lm_studio'",
        "$lmStudioParts.Port",
    ):
        assert marker in text

    assert "172\\.18\\.0\\.1:13666/v1" not in text
    assert "127.0.0.1:13666/v1" not in text


def test_authoritative_launcher_does_not_enable_projection_worker():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert 'BHM_PROJECTION_WORKER_ENABLED = "true"' not in text


def test_service_authoritative_switch_sets_complete_writer_gate_contract():
    text = SERVICE.read_text(encoding="utf-8")
    authoritative_block = text[
        text.index("if ($Authoritative)") : text.index("# Semantic fusion is never implicit")
    ]

    for marker in (
        'BHM_MEMORY_STORE_MODE = "sqlite-authoritative"',
        'BHM_FALLBACK_MODE = "explicit"',
        'BHM_PROJECTION_WORKER_ENABLED = "false"',
        'BHM_MEMORY_STORE_PARITY_CONFIRMED = "true"',
        'BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED = "true"',
    ):
        assert marker in authoritative_block


def test_service_inherits_operator_capability_from_user_scope_without_logging_it():
    text = SERVICE.read_text(encoding="utf-8")
    assert "BHM_ADMIN_CAPABILITY" in text
    assert "GetEnvironmentVariable('BHM_ADMIN_CAPABILITY', 'User')" in text
    assert "$env:BHM_ADMIN_CAPABILITY = $userAdminCapability" in text
    assert "Write-Host" not in text[text.index("BHM_ADMIN_CAPABILITY") : text.index("$apiParts")]
