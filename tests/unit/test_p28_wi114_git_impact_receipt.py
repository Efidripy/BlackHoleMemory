from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-p28-wi114-git-impact-receipt.py"
SPEC = importlib.util.spec_from_file_location("bhm_p28_wi114_git_impact_receipt", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _evidence(*, graph_digest: str = "graph-1") -> dict:
    return {
        "schema_version": "bhm.change-impact.git-symbols.v1",
        "graph_digest": graph_digest,
        "changed_paths": ["service.py"],
        "diff_hunks": [{"path": "service.py", "old_start": 1, "old_count": 1, "new_start": 1, "new_count": 1}],
        "hunk_symbols": [{"qualified_name": "service.route", "path": "service.py"}],
        "history_symbols": [{"qualified_name": "service.route", "path": "service.py"}],
        "git_history": {"commits_considered": 2},
        "provenance": {"git_metadata_only": True, "graph_metadata_only": True, "raw_source_returned": False},
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_mem0": False, "writes_worktree": False, "auto_apply": False},
    }


def test_git_impact_receipt_passes_aligned_bounded_proposal():
    first = MODULE.build_git_impact_receipt(_evidence(), current_graph_digest="graph-1")
    second = MODULE.build_git_impact_receipt(_evidence(), current_graph_digest="graph-1")

    assert first["ok"] is True
    assert first["status"] == "pass"
    assert first["checks"]["graph_digest_aligned"] is True
    assert first["coverage"]["diff_hunks"] == 1
    assert first["evidence_digest"] == second["evidence_digest"]


def test_git_impact_receipt_reports_missing_graph_binding_as_gap():
    receipt = MODULE.build_git_impact_receipt(_evidence())

    assert receipt["ok"] is False
    assert receipt["status"] == "gap"
    assert receipt["gaps"] == ["graph_digest_binding_missing"]
    assert receipt["failures"] == []


def test_git_impact_receipt_fails_digest_drift_and_unsafe_execution():
    evidence = _evidence(graph_digest="old")
    evidence["execution"]["auto_apply"] = True
    receipt = MODULE.build_git_impact_receipt(evidence, current_graph_digest="new")

    assert receipt["ok"] is False
    assert receipt["status"] == "fail"
    assert "graph_digest_mismatch" in receipt["failures"]
    assert "proposal_only_execution_boundary_failed" in receipt["failures"]


def test_git_impact_receipt_rejects_uncovered_hunk_and_raw_source():
    evidence = _evidence()
    evidence["diff_hunks"][0]["path"] = "outside.py"
    evidence["provenance"]["raw_source_returned"] = True
    receipt = MODULE.build_git_impact_receipt(evidence, current_graph_digest="graph-1")

    assert receipt["ok"] is False
    assert "hunk_path_outside_changed_paths" in receipt["failures"]
    assert "metadata_only_provenance_failed" in receipt["failures"]


def test_git_impact_validator_is_read_only_and_no_git_runner():
    text = SCRIPT.read_text(encoding="utf-8").lower()
    for marker in ("graph_digest_aligned", "writes_sqlite_state", "writes_qdrant", "writes_worktree", "raw_source_returned"):
        assert marker in text
    for forbidden in ("subprocess", "urlopen", "requests.", "apply_patch", "git -c"):
        assert forbidden not in text
