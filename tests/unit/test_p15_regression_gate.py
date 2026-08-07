from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-p15-regression.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("bhm_p15_regression_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_regression_gate_accepts_matching_contracts():
    gate = load_gate()
    baseline = {
        "route_count": 2,
        "routes_digest": "routes",
        "mcp_catalog": {"count": 1, "digest": "mcp"},
        "import_cycles": [],
        "api_behavior": {
            "/openapi.json": {"digest": "api", "path_count": 1, "operation_count": 1, "schema_count": 1, "version": "v"}
        },
    }
    current = {
        **baseline,
        "api_behavior": {
            "/openapi.json": {"digest": "api", "path_count": 1, "operation_count": 1, "schema_count": 1, "version": "v"},
            "/bhm/health": {"ok": True, "status": "healthy", "memory_store": "sqlite-authoritative"},
            "/health/cutover": {"ok": True, "graph": "compiled", "memory_store": "sqlite-authoritative"},
            "/bhm/health/slo": {"ok": True, "status": "healthy", "projection_pending": 0, "projection_failed": 0},
        },
    }
    result = gate.compare_contracts(
        baseline,
        current,
        {"totals": {"percent_covered": 70.0}},
        {"ok": True},
        {"ok": True},
    )
    assert result["ok"] is True
    assert result["failures"] == []


def test_regression_gate_fails_on_schema_and_coverage_drift():
    gate = load_gate()
    baseline = {"route_count": 1, "routes_digest": "a", "mcp_catalog": {"count": 1, "digest": "b"}, "import_cycles": [], "api_behavior": {"/openapi.json": {"digest": "c", "path_count": 1, "operation_count": 1, "schema_count": 1, "version": "v"}}}
    current = {**baseline, "routes_digest": "drift", "api_behavior": {"/openapi.json": {"digest": "drift", "path_count": 2, "operation_count": 1, "schema_count": 1, "version": "v"}}}
    result = gate.compare_contracts(baseline, current, {"totals": {"percent_covered": 10.0}}, {"ok": False}, {"ok": False})
    assert result["ok"] is False
    assert {"semantic_routes_digest", "openapi_digest", "coverage_floor", "startup_probe", "latency"}.issubset(result["failures"])


def test_regression_children_use_registry_process_bounds(monkeypatch):
    gate = load_gate()
    calls: list[float] = []

    def fake_run(*_args, **kwargs):
        calls.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True}), stderr="")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    assert gate.run_startup_probe()["ok"] is True
    assert gate.run_latency(2)["ok"] is True
    assert calls == [
        gate.PROCESS_EXECUTION_P15_STARTUP_TIMEOUT_SECONDS,
        gate.PROCESS_EXECUTION_P15_LATENCY_TIMEOUT_SECONDS,
    ]
