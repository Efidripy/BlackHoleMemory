from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _checker_module():
    path = Path(__file__).parents[2] / "scripts" / "check-undefined-globals.py"
    spec = importlib.util.spec_from_file_location("check_undefined_globals", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Keep this list explicit if the checker reveals a real pre-existing defect.
KNOWN_ISSUES: list[dict[str, object]] = [
    # Existing star-import boundary: these private helpers are not re-exported.
    {"module": "blackholememory.agents.developer_agent.executor_nodes", "scope": "quarantine_retrieve_node", "name": "_trim_context_for_llm"},
    {"module": "blackholememory.agents.developer_agent.executor_nodes", "scope": "generate_code_node", "name": "_trim_context_for_llm"},
    {"module": "blackholememory.agents.developer_agent.executor_nodes", "scope": "bhm_flush_context", "name": "_extract_upserted_id"},
]


def test_blackholememory_has_no_undefined_globals():
    result = _checker_module().check_package()
    errors = result["errors"]
    known = {(item["module"], item["scope"], item["name"]) for item in KNOWN_ISSUES}
    unexpected = [item for item in errors if (item["module"], item["scope"], item["name"]) not in known]
    assert not unexpected, f"undefined globals: {unexpected}"
