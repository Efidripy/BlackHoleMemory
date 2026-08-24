from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-bhm-auth-admin-parity.py"


def _load():
    spec = importlib.util.spec_from_file_location("validate_bhm_auth_admin_parity", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_auth_admin_parity_covers_all_static_interface_rows() -> None:
    module = _load()
    report = module.build_auth_admin_parity_report()

    assert report["ok"] is True
    assert report["inventory_row_count"] == 454
    assert report["classified_row_count"] == 454
    assert report["surface_counts"] == {"MCP_STATIC": 194, "REST/WS": 260}
    # Eight governed-consolidation tools are intentionally admin-only and
    # excluded from the static public MCP contract.
    assert report["mcp_registration_groups"] == {"admin": 83, "core": 35, "domain": 84}
    assert report["missing_live_interfaces"] == []
    assert report["unknown_mcp_tools"] == []
    assert report["implicit_route_policies"] == []
    assert report["duplicate_inventory_keys"] == []
    assert len(report["classification_digest"]) == 64


def test_websocket_inventory_row_is_classified_as_auth_only() -> None:
    module = _load()
    rows = module.load_inventory(module.DEFAULT_INVENTORY)
    names = module.registered_tool_names(ROOT / "src" / "blackholememory" / "bhm_mcp.py")
    groups = module.partition_registration_groups(names)
    tool_groups = {name: group for group, values in groups.items() for name in values}
    classified = next(
        module.classify_interface_row(row, tool_groups=tool_groups, routes=module.live_route_keys())
        for row in rows
        if row["operation"] == "WEBSOCKET"
    )

    assert classified["name"] == "/bhm/ws"
    assert classified["auth_policy"] == "project"
    assert classified["present"] is True
    assert classified["policy_explicit"] is True
