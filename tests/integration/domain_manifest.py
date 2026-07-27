"""Canonical logical domains for the BHM integration suite.

Every collected integration test is assigned exactly one bounded domain. The
manifest is deliberately test-only: it does not change application imports or
runtime behavior.
"""

from __future__ import annotations

from pathlib import Path


DOMAIN_NAMES = (
    "runtime",
    "mcp",
    "hooks",
    "observations",
    "storage",
    "crystallization",
    "retrieval",
    "web",
    "graph_ui",
    "agents",
    "quality",
)

MARKER_PREFIX = "bhm_"

FILE_DOMAINS = {
    "test_p12_mcp_attach_lease.py": "mcp",
    "test_p18_mcp_protocol_conformance.py": "mcp",
    "test_p12_retrieval_funnel.py": "retrieval",
    "test_p12_surface_report.py": "mcp",
    "test_p17_llm_delegation_surface.py": "mcp",
    "test_p13_native_projection_parity.py": "storage",
    "test_p13_qdrant_catalog.py": "storage",
    "test_p13_qdrant_lifecycle.py": "storage",
    "test_p13_qdrant_retention.py": "storage",
    "test_search_bounds.py": "retrieval",
    "test_wi01_repository_index.py": "storage",
    "test_wi02_code_graph.py": "storage",
    "test_wi03_code_graph_query.py": "storage",
    "test_wi34_change_impact.py": "storage",
    "test_wi04_convention_memory.py": "storage",
    "test_wi08_unified_context.py": "retrieval",
    "test_wi05_session_capture.py": "observations",
    "test_wi06_memory_graph.py": "storage",
    "test_wi07_task_graph.py": "storage",
    "test_wi09_llm_code_fabric.py": "mcp",
    "test_wi10_factories.py": "quality",
    "test_wi11_unified_mcp.py": "mcp",
    "test_p26_public_code_tools.py": "mcp",
    "test_p28_wi198_semantic_readiness_gate.py": "mcp",
    "test_wi13_capability_router.py": "agents",
    "test_wi12_human_ui.py": "graph_ui",
    "test_wi14_migration.py": "storage",
    "test_wi15_security.py": "observations",
    "test_wi17_final_acceptance.py": "quality",
    "test_caller_auth_boundary.py": "observations",
}

# Ordered from the most specific to the broad compatibility buckets.
PREFIX_DOMAINS = (
    (
        "retrieval",
        (
            "test_mcp_context_",
            "test_mcp_explain_",
            "test_mcp_memory_used_",
            "test_federated_",
            "test_lexical_",
            "test_rrf_",
            "test_hybrid_",
            "test_search_",
            "test_context_",
            "test_explain_",
            "test_memory_used_",
        ),
    ),
    (
        "runtime",
        (
            "test_async_profile",
            "test_profile_",
            "test_health_",
            "test_sqlite_authoritative_",
            "test_sqlite_",
            "test_public_openapi_",
            "test_project_registry_",
            "test_admin_openapi_",
            "test_required_storage_",
            "test_fallback_",
            "test_disabled_fallback",
            "test_pure_cutover_",
        ),
    ),
    (
        "mcp",
        (
            "test_mcp_",
            "test_admin_rest_route",
        ),
    ),
    (
        "hooks",
        (
            "test_hook_",
            "test_p1_9_hook_",
        ),
    ),
    (
        "observations",
        (
            "test_observation_",
            "test_observe_",
            "test_p1_9_observation_",
            "test_p1_9_secret_",
            "test_secret_",
            "test_galaxy_observation_",
            "test_memory_redaction",
            "test_app_observation_",
            "test_websocket_",
            "test_telemetry_",
        ),
    ),
    (
        "storage",
        (
            "test_sqlite_retention",
            "test_retention_",
            "test_reconciliation_",
            "test_active_zone_",
            "test_compress_zone_",
            "test_frozen_zone_",
            "test_tier4_",
        ),
    ),
    (
        "crystallization",
        (
            "test_compact_hook_",
        ),
    ),
    (
        "web",
        (
            "test_web_",
            "test_live_search_",
        ),
    ),
    (
        "graph_ui",
        (
            "test_graph_",
            "test_galaxy_",
        ),
    ),
    (
        "agents",
        (
            "test_speculative_",
            "test_swarm_",
            "test_supervisor_",
            "test_ast_",
            "test_scratchpad_",
            "test_infra_",
            "test_96_",
        ),
    ),
    (
        "quality",
        (
            "test_censor_",
            "test_data_hygiene_",
        ),
    ),
)


def marker_for(domain: str) -> str:
    if domain not in DOMAIN_NAMES:
        raise ValueError(f"unknown integration domain: {domain}")
    return f"{MARKER_PREFIX}{domain}"


def classify_test(file_name: str, test_name: str) -> str:
    """Return exactly one domain or raise for an unclassified test."""

    if file_name in FILE_DOMAINS:
        return FILE_DOMAINS[file_name]
    if file_name != "test_pure_core_features.py":
        raise ValueError(f"no integration domain mapping for {file_name}")
    for domain, prefixes in PREFIX_DOMAINS:
        if test_name.startswith(prefixes):
            return domain
    raise ValueError(f"no integration domain mapping for {file_name}::{test_name}")


def manifest_path() -> Path:
    return Path(__file__).resolve().parent
