from __future__ import annotations

from blackholememory.resource_limits import OPEN_BOUNDARY_FAMILIES
from blackholememory.resource_limits import PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_ARCHIVE_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_SIGNATURE_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_RELEASE_TRUST_GIT_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_SOURCE_REGISTRY_CLONE_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_SOURCE_REGISTRY_FETCH_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_DOCKER_CHECK_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_DOCKER_RECOVERY_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_P15_LATENCY_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_P15_STARTUP_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_LLM_INVENTORY_HARDWARE_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_GPU_SNAPSHOT_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS
from blackholememory.resource_limits import SQLITE_READINESS_PROBE_TIMEOUT_SECONDS
from blackholememory.resource_limits import SQLITE_HOOK_QUEUE_BUSY_TIMEOUT_SECONDS
from blackholememory.resource_limits import MCP_SESSION_ADMISSION_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import BHM_CODE_GRAPH_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import BHM_CODE_COVERAGE_PROBE_TIMEOUT_SECONDS
from blackholememory.resource_limits import BHM_CODE_GRAPH_SOFT_WAIT_SECONDS
from blackholememory.resource_limits import BHM_INDEX_MAX_FILES_PER_RUN
from blackholememory.resource_limits import BHM_FEDERATED_RETRIEVAL_CONTOUR_TIMEOUT_SECONDS
from blackholememory.resource_limits import BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS
from blackholememory.resource_limits import EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import LLM_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import LLM_REFLECTION_TIMEOUT_SECONDS
from blackholememory.resource_limits import LLM_INVENTORY_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import LLM_SECURITY_REVIEW_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_SERVICE_READINESS_POLL_SECONDS
from blackholememory.resource_limits import LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_TELEMETRY_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_UI_SESSION_MINT_TIMEOUT_SECONDS
from blackholememory.resource_limits import QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS
from blackholememory.resource_limits import RESOURCE_LIMITS
from blackholememory.resource_limits import resource_limit_snapshot
from blackholememory.resource_limits import validate_resource_limits
from blackholememory import app as bhm_app


def test_resource_limit_registry_is_unique_and_bounded() -> None:
    assert validate_resource_limits() == ()
    assert len(RESOURCE_LIMITS) >= 20
    assert all(item.value > 0 for item in RESOURCE_LIMITS)
    assert PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS == 30
    assert PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS == 60
    assert PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS == 120
    assert PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS == 30
    assert PROCESS_EXECUTION_OPERATOR_CONTROL_TIMEOUT_SECONDS == 15
    assert PROCESS_EXECUTION_DOCTOR_TIMEOUT_SECONDS == 60
    assert PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS == 5
    assert PROCESS_EXECUTION_RELEASE_TRUST_GIT_TIMEOUT_SECONDS == 5
    assert PROCESS_EXECUTION_RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS == 15
    assert PROCESS_EXECUTION_RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS == 10
    assert PROCESS_EXECUTION_RELEASE_ARCHIVE_TIMEOUT_SECONDS == 60
    assert PROCESS_EXECUTION_RELEASE_SIGNATURE_TIMEOUT_SECONDS == 30
    assert PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS == 30
    assert PROCESS_EXECUTION_SOURCE_REGISTRY_CLONE_TIMEOUT_SECONDS == 120
    assert PROCESS_EXECUTION_SOURCE_REGISTRY_FETCH_TIMEOUT_SECONDS == 120
    assert PROCESS_EXECUTION_CONTAINER_TIMEOUT_SECONDS == 15
    assert PROCESS_EXECUTION_DOCKER_CHECK_TIMEOUT_SECONDS == 3
    assert PROCESS_EXECUTION_DOCKER_RECOVERY_TIMEOUT_SECONDS == 20
    assert PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS == 2.0
    assert SQLITE_READINESS_PROBE_TIMEOUT_SECONDS == 1.0
    assert SQLITE_HOOK_QUEUE_BUSY_TIMEOUT_SECONDS == 5.0
    assert MCP_SESSION_ADMISSION_TIMEOUT_SECONDS == 30.0


def test_app_env_float_supports_upper_bounds(monkeypatch) -> None:
    monkeypatch.delenv("BHM_TEST_TIMEOUT", raising=False)
    assert bhm_app._env_float("BHM_TEST_TIMEOUT", 3.0, 0.1, 5.0) == 3.0
    monkeypatch.setenv("BHM_TEST_TIMEOUT", "9999")
    assert bhm_app._env_float("BHM_TEST_TIMEOUT", 3.0, 0.1, 5.0) == 5.0
    assert PROCESS_EXECUTION_P15_STARTUP_TIMEOUT_SECONDS == 20
    assert PROCESS_EXECUTION_P15_LATENCY_TIMEOUT_SECONDS == 90
    assert PROCESS_EXECUTION_LLM_INVENTORY_HARDWARE_TIMEOUT_SECONDS == 5
    assert PROCESS_EXECUTION_GPU_SNAPSHOT_TIMEOUT_SECONDS == 2
    assert PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS == 5
    assert BHM_INTERNAL_HTTP_TIMEOUT_SECONDS == 15
    assert BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS == 60
    assert BHM_CODE_GRAPH_HTTP_TIMEOUT_SECONDS == 120
    assert BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS == 30
    assert BHM_CODE_COVERAGE_PROBE_TIMEOUT_SECONDS == 5
    assert BHM_CODE_GRAPH_SOFT_WAIT_SECONDS == 20
    assert BHM_INDEX_MAX_FILES_PER_RUN == 666
    assert BHM_SPECULATIVE_SEARCH_TIMEOUT_SECONDS == 3
    assert BHM_FEDERATED_RETRIEVAL_CONTOUR_TIMEOUT_SECONDS == 3
    assert EXTERNAL_SEARCH_HTTP_TIMEOUT_SECONDS == 20
    assert LLM_HTTP_TIMEOUT_SECONDS == 120
    assert LLM_REFLECTION_TIMEOUT_SECONDS == 30
    assert LLM_INVENTORY_HTTP_TIMEOUT_SECONDS == 20
    assert LLM_SECURITY_REVIEW_TIMEOUT_SECONDS == 90
    assert LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS == 5.0
    assert LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS == 1.0
    assert LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS == 3.0
    assert LAUNCHER_TELEMETRY_TIMEOUT_SECONDS == 15.0
    assert LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS == 120.0
    assert LAUNCHER_SERVICE_READINESS_POLL_SECONDS == 1.0
    assert LAUNCHER_UI_SESSION_MINT_TIMEOUT_SECONDS == 4.0
    assert QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS == 30
    assert SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS == 45
    assert any(item.key == "process.execution_timeout" for item in RESOURCE_LIMITS)
    assert any(item.key == "mcp.session_admission_timeout" for item in RESOURCE_LIMITS)
    assert any(item.key == "repository.index_max_files_per_run" for item in RESOURCE_LIMITS)
    assert any(item.key == "retrieval.federated_contour_timeout" for item in RESOURCE_LIMITS)


def test_resource_limit_snapshot_is_deterministic_and_honest_about_open_families() -> None:
    first = resource_limit_snapshot()
    second = resource_limit_snapshot()
    assert first == second
    assert first["ok"] is True
    assert first["status"] == "baseline"
    assert set(OPEN_BOUNDARY_FAMILIES).issubset(first["open_boundary_families"])
    assert len(first["registry_sha256"]) == 64
