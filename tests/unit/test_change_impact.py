from __future__ import annotations

import pytest

from blackholememory.change_impact import ChangeImpactError, build_change_impact_preview, build_impact_binding_receipt
from blackholememory.change_impact import build_cross_repo_history_preview, build_git_history_correlation_receipt, build_git_symbol_impact_evidence
from blackholememory.change_impact import collect_git_diff_hunks
from blackholememory.change_impact import correlate_diff_hunks_to_symbols, summarize_diff_hunks
from blackholememory import change_impact
from blackholememory.filesystem_boundaries import FilesystemBoundaryError


def _snapshot() -> dict:
    return {
        "project": "demo",
        "graph_snapshot_id": "graph-1",
        "graph_digest": "digest-1",
        "nodes": [
            {"node_id": "f1", "stable_key": "file:service.py", "node_kind": "file", "path": "service.py", "language": "python", "name": "service.py"},
            {"node_id": "fn", "stable_key": "fn:service.route", "node_kind": "function", "path": "service.py", "language": "python", "name": "route"},
            {"node_id": "t1", "stable_key": "file:tests/test_service.py", "node_kind": "test", "path": "tests/test_service.py", "language": "python", "name": "test_service"},
        ],
        "edges": [
            {"stable_key": "contains:f1:fn", "edge_kind": "contains", "source_node_id": "f1", "target_node_id": "fn"},
            {"stable_key": "tests:t1:fn", "edge_kind": "tests", "source_node_id": "t1", "target_node_id": "fn"},
        ],
        "parse_results": [{"path": "service.py", "status": "parsed"}, {"path": "tests/test_service.py", "status": "parsed"}],
    }


def test_change_impact_builds_deterministic_ready_decision_card() -> None:
    conventions = {"stale": False, "cards": [{"card_id": "c1", "card_kind": "naming", "status": "proposal", "statement": "snake_case", "confidence": 0.9, "freshness_score": 1.0, "evidence": {"path_hashes": {"service.py": "h"}}}]}
    first = build_change_impact_preview(_snapshot(), ["service.py"], conventions=conventions)
    second = build_change_impact_preview(_snapshot(), ["service.py"], conventions=conventions)
    assert first["ready"] is True
    assert first["selectedTests"] == ["tests/test_service.py"]
    assert first["preview_digest"] == second["preview_digest"]
    assert first["execution"]["auto_apply"] is False


def test_change_impact_fails_closed_for_stale_conventions() -> None:
    result = build_change_impact_preview(_snapshot(), ["service.py"], conventions={"stale": True, "cards": []})
    assert result["ready"] is False
    assert "rebuild convention cards" in " ".join(result["whatWouldHelp"])


def test_change_impact_rejects_digest_drift_and_unsafe_path() -> None:
    with pytest.raises(ChangeImpactError, match="digest changed"):
        build_change_impact_preview(_snapshot(), ["service.py"], expected_graph_digest="other")
    with pytest.raises(ChangeImpactError, match="repository-relative"):
        build_change_impact_preview(_snapshot(), ["../outside.py"])


def test_git_context_rejects_reparse_repository_root(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError, match="symlink|junction|reparse"):
        change_impact._git_context(linked_root)


def test_change_impact_requires_all_paths_and_rejects_low_confidence() -> None:
    conventions = {"stale": False, "cards": [{"card_id": "weak", "card_kind": "naming", "status": "proposal", "statement": "uncertain", "confidence": 0.1, "evidence": {}}]}
    result = build_change_impact_preview(_snapshot(), ["service.py", "missing.py"], conventions=conventions)
    assert result["ready"] is False
    assert result["gates"]["coverage_complete"] is False
    assert result["gates"]["low_confidence_rejected"] is False


def test_change_impact_rejects_current_graph_digest_drift() -> None:
    snapshot = _snapshot()
    snapshot["current_graph_digest"] = "newer"
    result = build_change_impact_preview(snapshot, ["service.py"], conventions={"stale": False, "cards": []})
    assert result["ready"] is False
    assert result["graph_stale"] is True


def test_change_impact_rejects_explicit_stale_snapshot() -> None:
    snapshot = _snapshot()
    snapshot["stale"] = True
    result = build_change_impact_preview(snapshot, ["service.py"], conventions={"stale": False, "cards": []})
    assert result["ready"] is False
    assert result["graph_stale"] is True


def test_change_impact_rejects_stale_convention_evidence() -> None:
    result = build_change_impact_preview(
        _snapshot(),
        ["service.py"],
        conventions={"stale": False, "cards": [{"card_id": "old", "card_kind": "naming", "status": "proposal", "statement": "snake_case", "confidence": 0.95, "freshness_score": 0.2, "evidence": {}}]},
    )
    assert result["ready"] is False
    assert result["low_confidence"] is True
    assert "stale convention evidence" in " ".join(result["whatWouldHelp"])


def test_impact_binding_receipt_passes_with_graph_hunk_symbol_and_history_binding() -> None:
    hunks = [{"path": "service.py", "old_start": 2, "old_count": 1, "new_start": 2, "new_count": 1}]
    nodes = [
        {"node_id": "file", "stable_key": "file:service.py", "node_kind": "file", "path": "service.py", "start_line": 1, "end_line": 4},
        {"node_id": "route", "stable_key": "fn:service.route", "node_kind": "function", "path": "service.py", "name": "route", "start_line": 1, "end_line": 3},
    ]
    symbols = correlate_diff_hunks_to_symbols(hunks, nodes)
    first = build_impact_binding_receipt(
        graph_snapshot_id="graph-1",
        graph_digest="digest-1",
        expected_graph_digest="digest-1",
        changed_paths=["service.py"],
        diff_hunks=hunks,
        hunk_symbols=symbols,
        git_history={"commits_considered": 2},
        provenance={"git_metadata_only": True, "graph_metadata_only": True, "raw_source_returned": False},
        execution={"writes_sqlite_state": False, "auto_apply": False},
    )
    second = build_impact_binding_receipt(
        graph_snapshot_id="graph-1",
        graph_digest="digest-1",
        expected_graph_digest="digest-1",
        changed_paths=["service.py"],
        diff_hunks=hunks,
        hunk_symbols=symbols,
        git_history={"commits_considered": 2},
        provenance={"git_metadata_only": True, "graph_metadata_only": True, "raw_source_returned": False},
        execution={"writes_sqlite_state": False, "auto_apply": False},
    )
    assert first["status"] == "pass"
    assert first["ok"] is True
    assert first["graph_binding"]["aligned"] is True
    assert first["coverage"]["complete"] is True
    assert first["diff_summary"]["totals"]["change_kinds"]["replace"] == 1
    assert first["diff_summary"]["totals"]["added_lines"] == 1
    assert first["diff_summary"]["totals"]["removed_lines"] == 1
    assert first["receipt_digest"] == second["receipt_digest"]
    assert "signature" not in str(first)


def test_impact_binding_receipt_reports_missing_evidence_as_recoverable_gap() -> None:
    receipt = build_impact_binding_receipt(
        graph_snapshot_id="graph-1",
        graph_digest="digest-1",
        changed_paths=["service.py"],
        diff_hunks=[],
        hunk_symbols=[],
        git_history={"commits_considered": 0},
        provenance={"git_metadata_only": True, "graph_metadata_only": True, "raw_source_returned": False},
        execution={"writes_sqlite_state": False, "auto_apply": False},
    )
    assert receipt["status"] == "gap"
    assert receipt["ok"] is False
    assert "graph_digest_binding_missing" in receipt["gaps"]
    assert "diff_hunk_coverage_missing" in receipt["gaps"]
    assert receipt["failures"] == []


def test_impact_binding_receipt_fails_digest_drift_and_unsafe_markers() -> None:
    receipt = build_impact_binding_receipt(
        graph_snapshot_id="graph-1",
        graph_digest="old",
        expected_graph_digest="new",
        changed_paths=["service.py"],
        diff_hunks=[{"path": "outside.py", "old_start": 1, "old_count": 1, "new_start": 1, "new_count": 1}],
        hunk_symbols=[],
        git_history={"commits_considered": 1},
        provenance={"git_metadata_only": True, "graph_metadata_only": True, "raw_source_returned": False},
        execution={"writes_sqlite_state": False, "auto_apply": True, "edge_promotion": True},
    )
    assert receipt["status"] == "fail"
    assert "graph_digest_mismatch" in receipt["failures"]
    assert "hunk_path_outside_changed_paths" in receipt["failures"]
    assert "proposal_only_execution_boundary_failed" in receipt["failures"]


def test_git_diff_hunks_return_ranges_without_diff_text(tmp_path) -> None:
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    path = root / "service.py"
    path.write_text("def route():\n    return 1\n", encoding="utf-8")
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "service.py"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=fixture@example.test", "-c", "user.name=fixture", "commit", "-qm", "base"], check=True, env=env)
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env).stdout.strip()
    path.write_text("def route():\n    return 2\n", encoding="utf-8")
    hunks = collect_git_diff_hunks(root, base_revision=base, paths=["service.py"])
    assert hunks == [{"path": "service.py", "old_start": 2, "old_count": 1, "new_start": 2, "new_count": 1}]
    assert "return" not in str(hunks)


def test_git_metadata_commands_use_bounded_process_timeout(monkeypatch, tmp_path) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        return type("Completed", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(change_impact.subprocess, "run", fake_run)
    result = change_impact.collect_git_change_paths(tmp_path)
    assert result["paths"] == []
    assert len(calls) == 2
    change_impact.collect_git_history_stats(tmp_path, [])
    assert len(calls) == 3
    assert all(call["timeout"] == 30 for call in calls)


def test_diff_hunk_summary_is_deterministic_and_metadata_only() -> None:
    hunks = [
        {"path": "src/new.py", "old_start": 0, "old_count": 0, "new_start": 1, "new_count": 3},
        {"path": "src/old.py", "old_start": 1, "old_count": 2, "new_start": 1, "new_count": 0},
        {"path": "src/service.py", "old_start": 4, "old_count": 2, "new_start": 4, "new_count": 1},
        {"path": "../outside.py", "old_start": 1, "old_count": 1, "new_start": 1, "new_count": 1},
    ]
    first = summarize_diff_hunks(hunks)
    second = summarize_diff_hunks(hunks)
    assert first == second
    assert first["schema_version"] == "bhm.change-impact.hunk-summary.v1"
    assert first["totals"] == {
        "files": 3,
        "hunks": 3,
        "added_lines": 4,
        "removed_lines": 4,
        "net_lines": 0,
        "change_kinds": {"insert": 1, "delete": 1, "replace": 1, "empty": 0},
    }
    assert first["invalid_hunks"] == 1
    assert first["truncated"] is False
    assert "return 1" not in str(first)


def test_diff_hunks_correlate_only_metadata_symbol_spans() -> None:
    hunks = [{"path": "service.py", "old_start": 2, "old_count": 1, "new_start": 2, "new_count": 1}]
    nodes = [
        {"node_id": "file", "stable_key": "file:service.py", "node_kind": "file", "path": "service.py", "name": "service.py", "start_line": 1, "end_line": 4, "language": "python"},
        {"node_id": "route", "stable_key": "fn:service.route", "node_kind": "function", "path": "service.py", "name": "route", "qualified_name": "service.route", "start_line": 1, "end_line": 3, "language": "python", "signature": "return 1"},
        {"node_id": "other", "stable_key": "fn:service.other", "node_kind": "function", "path": "service.py", "name": "other", "start_line": 8, "end_line": 9, "language": "python"},
    ]
    first = correlate_diff_hunks_to_symbols(hunks, nodes)
    second = correlate_diff_hunks_to_symbols(hunks, nodes)
    assert {item["qualified_name"] for item in first} == {"", "service.route"}
    assert first == second
    assert all("signature" not in item for item in first)
    assert first[1]["hunks"][0]["match_scope"] == "both"


def test_git_symbol_impact_evidence_is_deterministic_and_read_only(tmp_path) -> None:
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    path = root / "service.py"
    companion = root / "tests.py"
    path.write_text("def route():\n    return 1\n", encoding="utf-8")
    companion.write_text("def test_route():\n    return route()\n", encoding="utf-8")
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=fixture@example.test", "-c", "user.name=fixture", "commit", "-qm", "base"], check=True, env=env)
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env).stdout.strip()
    path.write_text("def route():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "service.py"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=fixture@example.test", "-c", "user.name=fixture", "commit", "-qm", "change"], check=True, env=env)
    nodes = [
        {"node_id": "route", "stable_key": "fn:service.route", "node_kind": "function", "path": "service.py", "name": "route", "qualified_name": "service.route", "start_line": 1, "end_line": 2, "language": "python"},
        {"node_id": "test", "stable_key": "fn:tests.test_route", "node_kind": "function", "path": "tests.py", "name": "test_route", "qualified_name": "tests.test_route", "start_line": 1, "end_line": 2, "language": "python"},
    ]
    first = build_git_symbol_impact_evidence(root, ["service.py"], nodes, base_revision=base)
    second = build_git_symbol_impact_evidence(root, ["service.py"], nodes, base_revision=base)
    assert first == second
    assert first["hunk_symbols"][0]["qualified_name"] == "service.route"
    assert first["git_history"]["commits_considered"] == 2
    assert first["history_correlation"]["schema_version"] == "bhm.change-impact.git-history-correlation.v1"
    assert first["history_correlation"]["status"] == "pass"
    assert first["history_correlation"]["receipt_digest"] == second["history_correlation"]["receipt_digest"]
    assert first["provenance"]["raw_source_returned"] is False
    assert first["execution"]["writes_worktree"] is False
    assert "return 2" not in str(first)


def test_cross_repo_history_preview_is_bounded_proposal_only() -> None:
    evidence = [
        {"project": "alpha", "history": {"commits_considered": 3, "hotspots": [{"path": "src/service.py", "commits": 2}]}, "symbols": [{"qualified_name": "service.route", "stable_key": "a:route"}]},
        {"project": "beta", "history": {"commits_considered": 2, "hotspots": [{"path": "lib/service.py", "commits": 1}]}, "symbols": [{"qualified_name": "service.route", "stable_key": "b:route"}]},
    ]
    first = build_cross_repo_history_preview(evidence)
    second = build_cross_repo_history_preview(evidence)
    assert first["schema_version"] == "bhm.change-impact.cross-repo-history.v1"
    assert first["preview_digest"] == second["preview_digest"]
    assert {item["relation"] for item in first["proposals"]} == {"CROSS_GIT_HOTSPOT", "CROSS_GIT_SYMBOL"}
    assert first["repositories"][0]["history_correlation"]["schema_version"] == "bhm.change-impact.git-history-correlation.v1"
    assert first["repositories"][0]["history_correlation"]["status"] == "pass"
    assert first["provenance"]["authority"] == "proposal"
    assert first["execution"]["cross_edges_promoted"] is False
    assert first["execution"]["writes_sqlite_state"] is False


def test_git_history_correlation_receipt_is_bounded_and_fails_closed_without_signal() -> None:
    history = {
        "commits_considered": 4,
        "hotspots": [{"path": "src/service.py", "commits": 3}],
        "cochange": [{"changed_path": "src/service.py", "companion_path": "tests/service.py", "commits": 2}],
    }
    symbols = [{"relation": "cochange", "path": "tests/service.py", "stable_key": "test:service"}]
    receipt = build_git_history_correlation_receipt(history, symbols, changed_paths=["src/service.py"])
    assert receipt["status"] == "pass"
    assert receipt["counts"]["cochange_pairs"] == 1
    assert receipt["top_companions"] == [{"path": "tests/service.py", "weighted_commits": 2}]
    assert receipt["execution"]["auto_apply"] is False
    assert receipt["provenance"]["raw_source_returned"] is False

    gap = build_git_history_correlation_receipt({"commits_considered": 2, "hotspots": []}, changed_paths=["missing.py"])
    assert gap["status"] == "gap"
    assert "changed_path_history_missing" in gap["gaps"]
    assert gap["execution"]["writes_sqlite_state"] is False
