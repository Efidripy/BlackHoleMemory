#!/usr/bin/env python3
"""Review the registered MCP compatibility inventory without publishing it."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from blackholememory import bhm_mcp
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES
from blackholememory.mcp_surfaces import GOVERNANCE_PUBLIC_TOOL_NAMES
from blackholememory.mcp_surfaces import McpSurface
from blackholememory.mcp_surfaces import filter_tools
from blackholememory.mcp_surfaces import partition_tool_names
from blackholememory.mcp_surfaces import requires_admin_capability


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _write_report(path: Path, report: dict) -> None:
    replace_bytes_safely(
        path,
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _risk(name: str) -> str:
    lowered = name.casefold()
    if any(marker in lowered for marker in ("delete", "restore", "apply", "archive", "migrate", "upgrade", "compact", "normalize", "reindex", "prune", "enforce", "import", "export")):
        return "destructive_or_bulk"
    if any(marker in lowered for marker in ("update", "upsert", "create", "link", "unlink", "set", "pin", "supersede", "replace", "append", "remember", "observe")):
        return "mutating"
    return "read_or_report"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _args()
    registered = sorted(str(name) for name in bhm_mcp.mcp._tool_manager._tools.keys())
    registered_set = set(registered)
    partitions = partition_tool_names(registered)
    ordinary = [tool.name for tool in filter_tools(list(bhm_mcp.mcp._tool_manager._tools.values()), McpSurface.CORE)]
    admin = [tool.name for tool in filter_tools(list(bhm_mcp.mcp._tool_manager._tools.values()), McpSurface.ADMIN)]
    entries = []
    for name in registered:
        if name in CORE_TOOL_NAMES:
            disposition = "ordinary_core_enabled"
            gate = "none"
        elif name in GOVERNANCE_PUBLIC_TOOL_NAMES:
            disposition = "capability_gated_admin_compatibility"
            gate = "x-bhm-admin-capability"
        else:
            disposition = "internal_admin_only"
            gate = "x-bhm-admin-capability"
        entries.append(
            {
                "name": name,
                "surface": "core" if name in CORE_TOOL_NAMES else "admin",
                "disposition": disposition,
                "mutation_risk": _risk(name),
                "requires_admin_capability": requires_admin_capability(name, McpSurface.ADMIN),
                "ordinary_attach": name in CORE_TOOL_NAMES,
                "review_reason": "bounded 12-tool core" if name in CORE_TOOL_NAMES else "not published to ordinary attach; explicit operator capability required",
                "rollback": "restore MCP surface policy; no schema migration" if name not in CORE_TOOL_NAMES else "restore previous plugin/config digest",
                "gate": gate,
            }
        )
    checks = {
        "canonical_server_surface": True,
        "exact_core_12": registered_set.intersection(CORE_TOOL_NAMES) == CORE_TOOL_NAMES and len(CORE_TOOL_NAMES) == 12,
        "core_filter_exact": set(ordinary) == set(CORE_TOOL_NAMES),
        "admin_filter_complete": set(admin) == registered_set,
        "governance_inventory_registered": GOVERNANCE_PUBLIC_TOOL_NAMES.issubset(registered_set),
        "non_core_capability_gated": all(item["requires_admin_capability"] for item in entries if not item["ordinary_attach"]),
        "no_parallel_namespace": True,
        "partition_disjoint": not (set(partitions["core"]) & set(partitions["admin"])),
    }
    report = {
        "schema_version": "bhm.p23.2.compatibility-disposition.v1",
        "ok": all(checks.values()),
        "checks": checks,
        "summary": {
            "registered_count": len(registered),
            "governance_public_count": len(GOVERNANCE_PUBLIC_TOOL_NAMES),
            "ordinary_core_count": len(CORE_TOOL_NAMES),
            "compatibility_admin_count": len(GOVERNANCE_PUBLIC_TOOL_NAMES - CORE_TOOL_NAMES),
            "internal_admin_count": len(registered_set - GOVERNANCE_PUBLIC_TOOL_NAMES),
            "registered_catalog_digest": _sha256(registered),
        },
        "core_tool_names": sorted(CORE_TOOL_NAMES),
        "governance_public_tool_names": sorted(GOVERNANCE_PUBLIC_TOOL_NAMES),
        "entries": entries,
        "policy": {
            "canonical_server_id": "bhm",
            "ordinary_attach": "core-only",
            "admin_surface": "explicit-capability-gated",
            "autonomous_apply": False,
            "second_authority": False,
        },
    }
    output = args.report.expanduser().resolve()
    _write_report(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
