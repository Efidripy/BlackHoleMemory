from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-bhm-live-semantic.py"
SPEC = importlib.util.spec_from_file_location("wi180_live_semantic", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _result(*, active: bool = True, projection_only: bool = True, runtime_ok: bool = True) -> dict:
    return {
        "live": {
            "ok": runtime_ok,
            "runtime": {"ok": runtime_ok},
            "freshness": {"ok": runtime_ok},
            "semantic": {
                "state": "active" if active else "disabled",
                "active_queries": 1 if active else 0,
                "queries": [{"query": "workManager", "active": active, "projection_only": projection_only}],
            },
            "execution": {
                "writes_sqlite_state": False,
                "writes_qdrant": False,
                "model_started": False,
                "autonomous_apply": False,
                "raw_source_returned": False,
            },
        }
    }


def test_wi180_accepts_active_projection_only_live_receipt() -> None:
    gate = MODULE.evaluate_live_gate(_result())
    assert gate["ok"] is True
    assert gate["projection_only_rows"] == 1


def test_wi180_rejects_disabled_or_non_projection_receipt() -> None:
    disabled = MODULE.evaluate_live_gate(_result(active=False))
    non_projection = MODULE.evaluate_live_gate(_result(projection_only=False))
    assert disabled["ok"] is False
    assert "semantic_state_not_active" in disabled["failures"]
    assert non_projection["ok"] is False
    assert "semantic_rows_not_active_projection_only" in non_projection["failures"]
