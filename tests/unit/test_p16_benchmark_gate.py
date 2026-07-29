from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-p16.5-benchmark.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("bhm_p16_benchmark_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_p16_benchmark_proves_budget_reduction_without_quality_loss():
    gate = load_gate()
    result = gate.run_gate(count=100)
    assert result["ok"] is True
    assert result["quality_equal"] is True
    assert result["leakage_free"] is True
    assert result["token_cost_reduction"] >= 0.20
    assert result["baseline"]["quality"]["ndcg_at_5"] == result["tuned"]["quality"]["ndcg_at_5"] == 1.0


def test_p16_benchmark_does_not_emit_stress_content():
    gate = load_gate()
    result = gate.run_gate(count=100)
    assert "canonical evidence" not in str(result)
