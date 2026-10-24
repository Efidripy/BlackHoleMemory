"""Test helper (FIX-017): patch an imported dependency on the app module and every domain module that imports it."""

from __future__ import annotations

import importlib

_MODULES = [
    'app',
    'app_core',
    'app_core_fallback',
    'app_core_graph',
    'app_core_hit',
    'app_core_hook',
    'app_governed',
    'app_governed_base',
    'app_governed_semantic',
    'app_governed_shared',
    'app_graphs',
    'app_hooks',
    'app_lifecycle',
    'app_mcp',
    'app_ops',
    'app_ops_admin',
    'app_ops_entity',
    'app_ops_task',
    'app_ops_task2',
    'app_projects',
    'app_retrieval',
    'app_retrieval_support',
    'app_runtime',
    'app_telemetry',
    'code_tools_service',
    'code_tools_service_base',
    'code_tools_service_index',
    'code_tools_service_search',
    'code_tools_service_graph',
    'code_tools_service_analysis',
    'llm_routes',
    'galaxy_routes',
    'ui_routes',
    'memory_routes',
    'memory_routes_base',
    'memory_routes_read',
    'memory_routes_write',
    'code_tools_service_graph_build',
    'code_tools_service_scope',
    'app_lifecycle_hooks',
    'llm_routes_base',
    'llm_routes_jobs',
    'llm_routes_preview',
]


def patch_app_import(monkeypatch, name, value) -> None:
    hit = False
    for module_name in _MODULES:
        module = importlib.import_module(f"blackholememory.{module_name}")
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            hit = True
    if not hit:
        raise AttributeError(name)


def app_source_text() -> str:
    """Concatenated source of app.py and every app_* / route module (for tests that read the source as text)."""
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "src" / "blackholememory"
    names = (
        ["app.py"]
        + sorted(p.name for p in base.glob("app_*.py"))
        + sorted(p.name for p in base.glob("code_tools_service*.py"))
        + ["galaxy_routes.py", "ui_routes.py"]
        + sorted(p.name for p in base.glob("llm_routes*.py"))
        + sorted(p.name for p in base.glob("memory_routes*.py"))
    )
    return "\n".join((base / n).read_text(encoding="utf-8") for n in names if (base / n).exists())
