from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-bhm-defensive-proof-matrices.py"


def _load():
    spec = importlib.util.spec_from_file_location("bhm_defensive_proof_matrices", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_defensive_proof_matrices_are_deterministic_and_complete() -> None:
    module = _load()
    first = module.build_defensive_proof_matrices_report()
    second = module.build_defensive_proof_matrices_report()

    assert first["coverage_ok"] is True
    assert first["digest"] == second["digest"]
    assert len(first["interface_rows"]) == 448
    assert len(first["http_stateful_candidate_rows"]) == 178
    assert len(first["operator_mcp_tool_rows"]) == 156
    assert len(first["stateful_candidate_rows"]) == 334
    assert first["unresolved_interfaces"] == []
    assert first["unresolved_project_sinks"] == []
    assert first["unresolved_redaction_sinks"] == []
    assert all(row["owner"] and row["entry_boundary"] for row in first["interface_rows"])
    assert all(row["test_evidence_exists"] for row in first["project_sink_rows"] + first["redaction_sink_rows"])


def test_unclassified_interface_and_missing_boundary_signal_fail_closed() -> None:
    module = _load()
    assert module._unresolved([{"verified": False, "name": "synthetic-route"}]) == [
        {"verified": False, "name": "synthetic-route"}
    ]

    expectation = module.SinkExpectation(
        "synthetic-sink",
        "project",
        "sqlite_authority",
        "src/blackholememory/capability.py",
        "project_required",
        ("missing boundary signal",),
        "tests/unit/test_defensive_proof_matrices.py",
    )
    row = module._sink_rows((expectation,))[0]
    assert row["verified"] is False
    assert row["signals"] == {"missing boundary signal": False}
