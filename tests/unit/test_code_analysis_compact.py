from __future__ import annotations

import json

from blackholememory.code_tools_response_compact import compact_response
from blackholememory.api_models import PublicCodeToolRequest


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
