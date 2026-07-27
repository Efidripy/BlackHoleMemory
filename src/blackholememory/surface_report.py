"""Deterministic, non-destructive 80/20 reports for BHM agent surfaces."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

from .mcp_surfaces import CORE_TOOL_NAMES
from .mcp_surfaces import GOVERNANCE_PUBLIC_TOOL_NAMES
from .usage_telemetry import normalize_operation


SCHEMA_VERSION = 1
MAX_DESCRIPTION_LENGTH = 180
_COMPATIBILITY_RE = re.compile(r"compatibility|legacy", re.IGNORECASE)


def _tool_value(tool: Any, key: str) -> Any:
    if isinstance(tool, dict):
        return tool.get(key)
    return getattr(tool, key, None)


def _bounded_text(value: Any, limit: int = MAX_DESCRIPTION_LENGTH) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


def _usage_index(usage_snapshot: dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in (usage_snapshot or {}).get("operations") or []:
        if not isinstance(row, dict):
            continue
        surface = str(row.get("surface") or "other")
        operation = str(row.get("operation") or "other")
        index[(surface, operation)] = row
    return index


def _mcp_row(name: str, description: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    compatibility = bool(_COMPATIBILITY_RE.search(description))
    if compatibility:
        decision = "deprecate_candidate"
        reason_codes = ["explicit_compatibility_description", "review_before_change"]
    elif name in CORE_TOOL_NAMES:
        decision = "promote"
        reason_codes = ["core_attach_catalog", "low_surprise"]
    elif name in GOVERNANCE_PUBLIC_TOOL_NAMES:
        decision = "keep"
        reason_codes = ["governance_public", "compatibility_surface"]
    else:
        decision = "tuck"
        reason_codes = ["admin_or_extended_surface", "attach_budget"]
    return {
        "name": name,
        "decision": decision,
        "surface": "core" if name in CORE_TOOL_NAMES else ("public" if name in GOVERNANCE_PUBLIC_TOOL_NAMES else "admin"),
        "description": description,
        "observed_calls": int((usage or {}).get("count") or 0),
        "observed_error_rate": float((usage or {}).get("error_rate") or 0.0),
        "reason_codes": reason_codes,
        "review_required": decision == "deprecate_candidate",
        "deletion_allowed": False,
    }


def _openapi_operations(schema: dict[str, Any], usage: dict[tuple[str, str], dict[str, Any]], surface: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, path_item in sorted((schema.get("paths") or {}).items()):
        if not isinstance(path_item, dict):
            continue
        for method, operation in sorted(path_item.items()):
            if method.lower() not in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}:
                continue
            operation_label = normalize_operation(f"{method.upper()} {path}")
            observed = usage.get(("rest", operation_label)) or usage.get(("mcp", operation_label))
            deprecated = bool(isinstance(operation, dict) and operation.get("deprecated"))
            if deprecated:
                decision = "deprecate_candidate"
                reason_codes = ["explicit_openapi_deprecated", "review_before_change"]
            elif surface == "admin":
                decision = "tuck"
                reason_codes = ["capability_gated", "progressive_disclosure"]
            else:
                decision = "keep"
                reason_codes = ["public_rest_contract"]
            rows.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "operation": operation_label,
                    "surface": surface,
                    "decision": decision,
                    "operation_id": _bounded_text((operation or {}).get("operationId") if isinstance(operation, dict) else "", 96),
                    "observed_calls": int((observed or {}).get("count") or 0),
                    "observed_error_rate": float((observed or {}).get("error_rate") or 0.0),
                    "reason_codes": reason_codes,
                    "review_required": decision == "deprecate_candidate",
                    "deletion_allowed": False,
                }
            )
    return rows


def _top_twenty_percent(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (-int(row.get("observed_calls") or 0), str(row.get("name") or row.get("operation") or "")))
    total_calls = sum(int(row.get("observed_calls") or 0) for row in rows)
    top_count = max(1, math.ceil(len(rows) * 0.2)) if rows else 0
    top_rows = ordered[:top_count]
    top_calls = sum(int(row.get("observed_calls") or 0) for row in top_rows)
    return {
        "row_count": len(rows),
        "top_20_percent_count": top_count,
        "observed_calls": total_calls,
        "top_20_percent_calls": top_calls,
        "top_20_percent_call_share": round(top_calls / total_calls, 6) if total_calls else 0.0,
        "top_rows": [
            {
                "name": row.get("name") or row.get("operation"),
                "surface": row.get("surface"),
                "observed_calls": int(row.get("observed_calls") or 0),
            }
            for row in top_rows
        ],
    }


def build_surface_report(
    *,
    mcp_tools: Iterable[Any],
    public_openapi: dict[str, Any],
    admin_openapi: dict[str, Any],
    usage_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded recommendation report without mutating registrations."""

    usage = _usage_index(usage_snapshot)
    mcp_rows = []
    for tool in mcp_tools:
        name = _bounded_text(_tool_value(tool, "name"), 96)
        if not name:
            continue
        description = _bounded_text(_tool_value(tool, "description"))
        mcp_rows.append(_mcp_row(name, description, usage.get(("mcp", f"tools/call:{name}"))))
    mcp_rows.sort(key=lambda row: row["name"])

    public_rows = _openapi_operations(public_openapi, usage, "public")
    all_rows = _openapi_operations(admin_openapi, usage, "admin")
    public_keys = {(row["method"], row["path"]) for row in public_rows}
    admin_rows = [row for row in all_rows if (row["method"], row["path"]) not in public_keys]
    openapi_rows = [*public_rows, *admin_rows]
    openapi_rows.sort(key=lambda row: (row["path"], row["method"]))

    deprecate_candidates = [
        {"surface": "mcp", "name": row["name"], "reason_codes": row["reason_codes"]}
        for row in mcp_rows
        if row["decision"] == "deprecate_candidate"
    ]
    deprecate_candidates.extend(
        {
            "surface": "openapi",
            "name": f"{row['method']} {row['path']}",
            "reason_codes": row["reason_codes"],
        }
        for row in openapi_rows
        if row["decision"] == "deprecate_candidate"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": {
            "mode": "recommendation_only",
            "deletion_allowed": False,
            "compatibility_window_required": True,
            "review_required_before_deprecate": True,
            "usage_window": (usage_snapshot or {}).get("window") or {"kind": "unavailable"},
        },
        "inventory": {
            "mcp_registered": len(mcp_rows),
            "mcp_promote": sum(row["decision"] == "promote" for row in mcp_rows),
            "mcp_keep": sum(row["decision"] == "keep" for row in mcp_rows),
            "mcp_tuck": sum(row["decision"] == "tuck" for row in mcp_rows),
            "mcp_deprecate_candidates": sum(row["decision"] == "deprecate_candidate" for row in mcp_rows),
            "openapi_operations": len(openapi_rows),
            "openapi_public": len(public_rows),
            "openapi_admin_only": len(admin_rows),
        },
        "eighty_twenty": {
            "mcp": _top_twenty_percent(mcp_rows),
            "openapi": _top_twenty_percent(openapi_rows),
        },
        "deprecate_candidates": deprecate_candidates,
        "mcp": mcp_rows,
        "openapi": openapi_rows,
    }


__all__ = ["build_surface_report"]
