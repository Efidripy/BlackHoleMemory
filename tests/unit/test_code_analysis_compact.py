from __future__ import annotations

import json
import asyncio

from blackholememory.code_tools_response_compact import bound_coverage_response, compact_response
from blackholememory.api_models import PublicCodeToolRequest
from blackholememory import code_tools_service_analysis as analysis


def test_compact_detail_and_pagination():
    original = {"parser_capabilities": {"languages": [{"name": str(i), "description": "x" * 500} for i in range(143)]}, "architecture": {"hotspots": [{"id": i} for i in range(70)], "layers": [{"id": i} for i in range(70)], "dependencies": [{"id": i} for i in range(70)]}, "index": {"delta": {"added": list(range(70)), "changed": list(range(70)), "removed": list(range(70))}}}
    first = compact_response(original, offset=0, limit=10)
    second = compact_response(original, offset=10, limit=10)
    full = compact_response(original, offset=0, limit=10, full=True)
    assert len(json.dumps(first)) < 20_000
    assert first["parser_capabilities"]["languages"]["count"] == 143
    assert full["parser_capabilities"]["languages"] == original["parser_capabilities"]["languages"][:10]
    assert full["parser_capabilities"]["pagination"]["languages"]["total"] == 143
    for container, key in [("architecture", "hotspots"), ("architecture", "layers"), ("architecture", "dependencies")]:
        assert first[container]["pagination"][key]["total"] == 70
        assert first[container]["pagination"][key]["next_offset"] == 10
        assert second[container][key] == original[container][key][10:20]
    for key in ("added", "changed", "removed"):
        assert first["index"]["delta"]["pagination"][key]["next_offset"] == 10
        assert second["index"]["delta"][key] == original["index"]["delta"][key][10:20]
    assert PublicCodeToolRequest(operation="coverage").detail == "compact"


def test_coverage_compact_bounds_large_pages_and_retains_continuation():
    original = {"index": {"delta": {"added": ["x" * 5000 + str(i) for i in range(50)]}}, "coverage": {"complete": True}}
    response = bound_coverage_response(compact_response(original, offset=10, limit=20))
    page = response["index"]["delta"]
    assert len(json.dumps(response)) < 20_000
    assert response["truncated"] is True
    assert page["pagination"]["added"]["truncated"] is True
    assert page["pagination"]["added"]["total"] == 50
    assert page["pagination"]["added"]["next_offset"] == 10 + len(page["added"])
    assert page["added"] == original["index"]["delta"]["added"][10:10 + len(page["added"])]
    assert response["coverage"]["complete"] is True


def test_coverage_compact_bounds_large_scalar():
    response = bound_coverage_response({"operation": "coverage", "project": "x" * 100_000})
    assert len(json.dumps(response)) < 20_000
    assert response["truncated"] is True


def test_architecture_full_preserves_large_nested_metadata(monkeypatch):
    async def scope(*args):
        return "unused", None, "project", "root", "root-id"

    async def read(label, *args, **kwargs):
        if label == "code.graph.current":
            return {"graph_snapshot_id": "snapshot", "summary": {}}
        return {"nodes": [], "edges": []}

    class Store:
        def __init__(self, *args):
            pass

        def snapshot(self, *args, **kwargs):
            pass

    # 150 KiB: full keeps nested data while it fits the 240 KiB transport cap; larger payloads are bounded (integration test).
    large = {"items": [{"text": "x" * 1000} for _ in range(150)]}
    monkeypatch.setattr(analysis._base, "_resolve_code_tool_scope", scope)
    monkeypatch.setattr(analysis, "_run_bounded_read", read)
    monkeypatch.setattr(analysis, "SQLiteCodeGraphStore", Store)
    monkeypatch.setattr(analysis, "build_architecture_intelligence", lambda *a, **k: dict(large))
    monkeypatch.setattr(analysis, "build_architecture_explain_receipt", lambda *a, **k: {})
    monkeypatch.setattr(analysis, "build_graph_analysis_quality_receipt", lambda *a, **k: {})
    monkeypatch.setattr(analysis, "build_architecture_memory", lambda *a, **k: {})
    request = PublicCodeToolRequest(operation="architecture", detail="full", limit=10)
    response = asyncio.run(analysis._op_architecture(request))
    assert response["architecture"]["intelligence"]["items"] == large["items"]
    assert response["architecture"]["pagination"]["layers"]["limit"] == 10
