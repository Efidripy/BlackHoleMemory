#!/usr/bin/env python
"""Build deterministic, source-only defensive proof matrices for BHM.

The validator intentionally distinguishes interface classification from runtime
proof.  It covers every frozen REST/WS/MCP interface row, a declared registry
of project-bearing sinks, and a declared registry of sensitive-data sinks.
Changing an interface inventory row or removing a required boundary signal
fails closed until the owner explicitly classifies it and adds evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "bhm.defensive-proof-matrices.v1"


@dataclass(frozen=True)
class SinkExpectation:
    sink_id: str
    matrix: str
    family: str
    path: str
    scope_mode: str
    boundary_signals: tuple[str, ...]
    test_evidence: str


PROJECT_SINKS = (
    SinkExpectation("sqlite-memory", "project", "sqlite_authority", "src/blackholememory/memory_repository.py", "project_filtered", ("m.project = ?", "memory_id = ? AND m.project = ?"), "tests/unit/test_project_scope.py"),
    SinkExpectation("qdrant-projection", "project", "qdrant_projection", "src/blackholememory/qdrant_projector.py", "project_filtered", ('"project": memory.project', "projection_payload_digest"), "tests/unit/test_project_scope.py"),
    SinkExpectation("mem0-projection", "project", "mem0", "src/blackholememory/mem0_adapter.py", "project_filtered", ("project=", "collection"), "tests/unit/test_project_scope.py"),
    SinkExpectation("llm-jobs", "project", "llm_job_sqlite", "src/blackholememory/llm_job_queue.py", "project_filtered", ("_scoped_idempotency_key", "AND project = ?"), "tests/unit/test_stream_e_integrity.py"),
    SinkExpectation("hook-queue", "project", "hook_queue_sqlite", "src/blackholememory/hook_queue.py", "project_filtered", ("_scoped_event_id", "AND project = ?"), "tests/unit/test_stream_e_integrity.py"),
    SinkExpectation("observations", "project", "observation_sqlite", "src/blackholememory/observation_store.py", "project_filtered", ("_scoped_event_id", "e.project = ?"), "tests/unit/test_stream_e_integrity.py"),
    SinkExpectation("repository-index", "project", "repository_index_sqlite", "src/blackholememory/repository_index.py", "project_filtered", ("current.project = ? AND current.root_id = ?", "snapshot.get(\"project\")"), "tests/unit/test_project_scope.py"),
    SinkExpectation("code-graph", "project", "code_graph_sqlite", "src/blackholememory/code_graph.py", "project_filtered", ("WHERE current.project=? AND current.root_id=?", "snapshot.get(\"project\")"), "tests/unit/test_project_scope.py"),
    SinkExpectation("admin-export", "project", "export", "src/blackholememory/app.py", "project_required", ("/bhm/admin/export", "caller_project_scope_requires_explicit"), "tests/unit/test_admin_snapshot_contract.py"),
    SinkExpectation("llm-cache-status", "project", "llm_cache", "src/blackholememory/app.py", "auth_only_aggregate", ("def bhm_llm_cache_status", "without returning payloads"), "tests/integration/test_caller_auth_boundary.py"),
)

REDACTION_SINKS = (
    SinkExpectation("observation-ingress", "redaction", "observation_sqlite", "src/blackholememory/app.py", "sanitized_before_write", ("_secure_observation_request_model", "_append_observation(item)"), "tests/integration/test_pure_core_features.py"),
    SinkExpectation("hook-ingress", "redaction", "hook_queue_sqlite", "src/blackholememory/app.py", "sanitized_before_enqueue", ("_secure_observation_request_model", "_enqueue_hook_request"), "tests/integration/test_pure_core_features.py"),
    SinkExpectation("jsonrpc-result", "redaction", "mcp_response", "src/blackholememory/app.py", "recursive_sanitized", ("def _jsonrpc_success", "PayloadSanitizer(max_collection_items=2_000).sanitize(result)"), "tests/unit/test_mcp_streamable_http.py"),
    SinkExpectation("jsonrpc-error", "redaction", "mcp_error", "src/blackholememory/app.py", "text_redacted", ("def _jsonrpc_error", "redact_secret_text"), "tests/unit/test_mcp_streamable_http.py"),
    SinkExpectation("llm-ingress", "redaction", "llm_job_sqlite", "src/blackholememory/app.py", "sanitized_before_write", ("sanitize_llm_value", "stored_payload"), "tests/unit/test_llm_safety.py"),
    SinkExpectation("llm-proposal", "redaction", "llm_output", "src/blackholememory/llm_safety.py", "recursive_sanitized", ("def build_proposal_envelope", "sanitize_llm_value(output"), "tests/unit/test_llm_safety.py"),
    SinkExpectation("langgraph-checkpoint", "redaction", "checkpoint_sqlite", "src/blackholememory/langgraph_checkpoint.py", "sanitized_before_write", ("self._dump(_redact(values[channel]", "self._dump(_redact(value, key=channel_name))"), "tests/unit/test_langgraph_checkpoint.py"),
    SinkExpectation("source-registry", "redaction", "json_file", "src/blackholememory/source_registry.py", "sanitized_before_write", ("_redact_persisted_payload", "_redact_source_url"), "tests/unit/test_source_registry_web_transport.py"),
    SinkExpectation("mcp-broker", "redaction", "mcp_error", "src/blackholememory/infra/mcp_broker.py", "text_redacted", ("redact_secret_text",), "tests/unit/test_mcp_broker_limits.py"),
)


def _load_auth_admin_module() -> Any:
    path = REPO_ROOT / "scripts" / "validate-bhm-auth-admin-parity.py"
    spec = importlib.util.spec_from_file_location("bhm_auth_admin_parity", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("auth_admin_parity_loader_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_stateful(row: dict[str, Any]) -> bool:
    if row["surface"] == "REST/WS":
        return row["operation"] in {"POST", "PUT", "PATCH", "DELETE"}
    return bool(row["admin_capability_required"])


def _interface_rows() -> list[dict[str, Any]]:
    module = _load_auth_admin_module()
    source_rows = module.load_inventory(module.DEFAULT_INVENTORY)
    names = module.registered_tool_names(REPO_ROOT / "src" / "blackholememory" / "bhm_mcp.py")
    groups = module.partition_registration_groups(names)
    tool_groups = {name: group for group, values in groups.items() for name in values}
    routes = module.live_route_keys()
    result: list[dict[str, Any]] = []
    for item in source_rows:
        classified = module.classify_interface_row(item, tool_groups=tool_groups, routes=routes)
        stateful = _is_stateful(classified)
        gate = "admin_capability" if classified["admin_capability_required"] else classified["auth_policy"]
        result.append(
            {
                **classified,
                "stateful_candidate": stateful,
                "owner": "mcp_gateway" if classified["surface"] == "MCP_STATIC" else "http_middleware",
                "entry_boundary": gate,
                "verified": bool(classified["present"] and classified["policy_explicit"] and classified["publication_group"] != "unknown"),
            }
        )
    return sorted(result, key=lambda row: (row["surface"], row["operation"], row["name"]))


def _sink_rows(expectations: tuple[SinkExpectation, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for expectation in expectations:
        source = (REPO_ROOT / expectation.path).read_text(encoding="utf-8")
        signals = {signal: signal in source for signal in expectation.boundary_signals}
        evidence_exists = (REPO_ROOT / expectation.test_evidence).is_file()
        rows.append(
            {
                **asdict(expectation),
                "signals": signals,
                "test_evidence_exists": evidence_exists,
                "verified": all(signals.values()) and evidence_exists,
            }
        )
    return rows


def _unresolved(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if not row["verified"]]


def build_defensive_proof_matrices_report() -> dict[str, Any]:
    interfaces = _interface_rows()
    http_stateful = [
        row for row in interfaces if row["surface"] == "REST/WS" and row["stateful_candidate"]
    ]
    operator_mcp_tools = [
        row for row in interfaces if row["surface"] == "MCP_STATIC" and row["stateful_candidate"]
    ]
    project_sinks = _sink_rows(PROJECT_SINKS)
    redaction_sinks = _sink_rows(REDACTION_SINKS)
    unresolved_interfaces = _unresolved(interfaces)
    unresolved_project = _unresolved(project_sinks)
    unresolved_redaction = _unresolved(redaction_sinks)
    canonical = json.dumps(
        {"interfaces": interfaces, "project_sinks": project_sinks, "redaction_sinks": redaction_sinks},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "writes_live_state": False,
        "interface_rows": interfaces,
        "http_stateful_candidate_rows": http_stateful,
        "operator_mcp_tool_rows": operator_mcp_tools,
        "stateful_candidate_rows": [*operator_mcp_tools, *http_stateful],
        "project_sink_rows": project_sinks,
        "redaction_sink_rows": redaction_sinks,
        "unresolved_interfaces": unresolved_interfaces,
        "unresolved_project_sinks": unresolved_project,
        "unresolved_redaction_sinks": unresolved_redaction,
        "coverage_ok": not (unresolved_interfaces or unresolved_project or unresolved_redaction),
        "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def parse_args() -> argparse.Namespace:
    return argparse.ArgumentParser(description=__doc__).parse_args()


def main() -> int:
    parse_args()
    report = build_defensive_proof_matrices_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["coverage_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
