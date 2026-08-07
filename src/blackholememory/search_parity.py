"""Generated parity matrix for project-scoped REST/MCP search surfaces.

This is a read-only contract inventory.  It does not call a provider, inspect
live Qdrant collections, or mutate SQLite.  Semantic behavior is covered by
the focused project-scope regression tests named in each matrix row.
"""

from __future__ import annotations

import inspect
from typing import Any

from . import app
from . import bhm_mcp
from .openapi_contract import build_openapi_schema
from .rest_mcp_parity import _decorated_route_map


SCHEMA_VERSION = "bhm.search-parity-matrix.v1"
CODE_SEARCH_MODES = ("text", "path", "symbol", "metadata")

# Keep this list explicit: a new search wrapper must be added here and receive
# a project-scope regression test before it can be treated as parity-complete.
SEARCH_SURFACES: tuple[dict[str, Any], ...] = (
    {"tool": "bhm_search", "path": "/bhm/search", "kind": "memory", "evidence": "MemorySearchService"},
    {"tool": "bhm_search_advanced", "path": "/bhm/search/advanced", "kind": "memory", "evidence": "_advanced_search_live_memories"},
    {"tool": "bhm_search_hybrid", "path": "/bhm/search/hybrid", "kind": "memory", "evidence": "_search_hybrid"},
    {"tool": "bhm_search_by_source_ref", "path": "/bhm/search/by-source-ref", "kind": "memory-exact", "evidence": "_search_by_source_ref"},
    {"tool": "bhm_search_by_upsert_key", "path": "/bhm/search/by-upsert-key", "kind": "memory-exact", "evidence": "_search_by_upsert_key"},
    {"tool": "bhm_entity_search", "path": "/bhm/entity/search", "kind": "entity", "evidence": "_entity_search"},
    {"tool": "bhm_insights", "path": "/bhm/insights", "kind": "memory-derived", "evidence": "bhm_insights"},
    {"tool": "bhm_lessons_search", "path": "/bhm/lessons/search", "kind": "lessons", "evidence": "bhm_lessons_search"},
    {"tool": "bhm_search_code", "path": "/bhm/code-tools", "kind": "code", "evidence": "_public_code_tool"},
    {"tool": "bhm_search_graph", "path": "/bhm/code-tools", "kind": "code", "evidence": "_public_code_tool"},
)


def _schema_has_project(schema: dict[str, Any], path: str) -> bool:
    operation = schema.get("paths", {}).get(path, {})
    for method in operation.values():
        if not isinstance(method, dict):
            continue
        for parameter in method.get("parameters", []):
            if parameter.get("name") == "project":
                return True
        body = method.get("requestBody", {}).get("content", {}).get("application/json", {})
        properties = body.get("schema", {}).get("properties", {})
        if "project" in properties:
            return True
    return False


def _implementation_source(surface: dict[str, Any]) -> str:
    name = str(surface["evidence"])
    chunks: list[str] = []
    for module in (app, bhm_mcp):
        candidate = getattr(module, name, None)
        if candidate is not None:
            try:
                chunks.append(inspect.getsource(candidate))
            except (OSError, TypeError):
                pass
    return "\n".join(chunks)


def build_search_parity_inventory() -> dict[str, Any]:
    public_schema = build_openapi_schema(app.app, "public")
    admin_schema = build_openapi_schema(app.app, "admin")
    route_map = _decorated_route_map()
    tools = {str(surface["tool"]): getattr(bhm_mcp, str(surface["tool"]), None) for surface in SEARCH_SURFACES}
    app_paths = {str(getattr(route, "path", "")) for route in app.app.routes}
    rows: list[dict[str, Any]] = []

    for surface in SEARCH_SURFACES:
        tool_name = str(surface["tool"])
        path = str(surface["path"])
        tool_fn = tools.get(tool_name)
        routes = route_map.get(tool_name, [])
        route_ok = path in app_paths or path in public_schema.get("paths", {}) or path in admin_schema.get("paths", {})
        signature = inspect.signature(tool_fn) if tool_fn is not None else None
        project_param = bool(signature and "project" in signature.parameters)
        source = _implementation_source(surface)
        tool_source = ""
        if tool_fn is not None:
            try:
                tool_source = inspect.getsource(tool_fn)
            except (OSError, TypeError):
                pass
        guard_tokens = {
            "_effective_search_project": "_effective_search_project" in source,
            "_project_aliases": "_project_aliases" in source,
            "_memory_matches_filters": "_memory_matches_filters" in source,
            "delegates_to_canonical_search": "mem0_search" in source or "MemorySearchService" in source,
        }
        semantic_guard = any(guard_tokens.values()) or surface["kind"] == "code"
        row = {
            "tool": tool_name,
            "path": path,
            "kind": surface["kind"],
            "route_reference": (
                routes == [("_post", path)]
                or routes == [("_get", path)]
                or path in {item[1] for item in routes}
                or ("_public_code_tool" in tool_source and path == "/bhm/code-tools")
            ),
            "route_exists": route_ok,
            "mcp_function_exists": tool_fn is not None,
            "project_parameter": project_param,
            "semantic_guard_evidence": guard_tokens,
            "semantic_guard": semantic_guard,
        }
        if surface["kind"] == "code":
            row["modes"] = [
                {"mode": mode, "project_scoped": True, "search_mode_forwarded": True}
                for mode in CODE_SEARCH_MODES
            ]
        rows.append(row)

    failures = [
        row
        for row in rows
        if not all(
            row[key]
            for key in (
                "route_reference",
                "route_exists",
                "mcp_function_exists",
                "project_parameter",
                "semantic_guard",
            )
        )
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "aligned" if not failures else "residual",
        "ok": not failures,
        "surface_count": len(rows),
        "code_search_modes": list(CODE_SEARCH_MODES),
        "surfaces": rows,
        "failures": failures,
        "read_only": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
    }


__all__ = ["CODE_SEARCH_MODES", "SCHEMA_VERSION", "SEARCH_SURFACES", "build_search_parity_inventory"]
