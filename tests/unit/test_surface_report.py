from __future__ import annotations

from types import SimpleNamespace

from blackholememory.surface_report import build_surface_report


def test_surface_report_is_deterministic_and_non_destructive():
    tools = [
        SimpleNamespace(name="bhm_search", description="Search live memory."),
        SimpleNamespace(name="bhm_batch_upsert_memories", description="Compatibility JSON wrapper."),
        SimpleNamespace(name="bhm_admin_export", description="Export operator snapshot."),
    ]
    usage = {
        "window": {"kind": "process", "started_at": "fixed"},
        "operations": [
            {"surface": "mcp", "operation": "tools/call:bhm_search", "count": 8, "error_rate": 0.0},
            {"surface": "rest", "operation": "GET_/bhm/search", "count": 3, "error_rate": 0.1},
        ],
    }
    public = {
        "paths": {
            "/bhm/search": {"post": {"operationId": "bhm_search"}},
            "/bhm/health": {"get": {"operationId": "bhm_health"}},
        }
    }
    admin = {
        "paths": {
            **public["paths"],
            "/bhm/memory/hard": {"delete": {"operationId": "bhm_memory_hard"}},
        }
    }

    report = build_surface_report(
        mcp_tools=tools,
        public_openapi=public,
        admin_openapi=admin,
        usage_snapshot=usage,
    )

    assert report["policy"]["deletion_allowed"] is False
    assert report["inventory"] == {
        "mcp_registered": 3,
        "mcp_promote": 1,
        "mcp_keep": 0,
        "mcp_tuck": 1,
        "mcp_deprecate_candidates": 1,
        "openapi_operations": 3,
        "openapi_public": 2,
        "openapi_admin_only": 1,
    }
    assert report["eighty_twenty"]["mcp"]["top_20_percent_calls"] == 8
    assert report["eighty_twenty"]["mcp"]["top_20_percent_call_share"] == 1.0
    assert report["deprecate_candidates"] == [
        {
            "surface": "mcp",
            "name": "bhm_batch_upsert_memories",
            "reason_codes": ["explicit_compatibility_description", "review_before_change"],
        }
    ]
    assert report["openapi"][0]["deletion_allowed"] is False

