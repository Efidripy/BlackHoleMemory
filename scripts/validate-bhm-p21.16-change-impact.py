#!/usr/bin/env python3
"""Produce bounded P21.16 change-impact/edit-preflight evidence."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from blackholememory.change_impact import ChangeImpactError, build_change_impact_preview, collect_git_change_paths, collect_git_history_stats
from blackholememory.filesystem_boundaries import replace_bytes_safely


def _write_report(path: Path, report: dict) -> None:
    replace_bytes_safely(path, (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _snapshot() -> dict:
    return {
        "project": "p21.16",
        "graph_snapshot_id": "graph-p21.16",
        "graph_digest": "graph-digest-p21.16",
        "nodes": [
            {"node_id": "f1", "stable_key": "file:src/service.py", "node_kind": "file", "path": "src/service.py", "language": "python", "name": "service.py"},
            {"node_id": "fn", "stable_key": "fn:src/service.route", "node_kind": "function", "path": "src/service.py", "language": "python", "name": "route"},
            {"node_id": "t1", "stable_key": "file:tests/test_service.py", "node_kind": "test", "path": "tests/test_service.py", "language": "python", "name": "test_service"},
        ],
        "edges": [
            {"stable_key": "contains:f1:fn", "edge_kind": "contains", "source_node_id": "f1", "target_node_id": "fn"},
            {"stable_key": "tests:t1:fn", "edge_kind": "tests", "source_node_id": "t1", "target_node_id": "fn"},
        ],
        "parse_results": [{"path": "src/service.py", "status": "parsed"}, {"path": "tests/test_service.py", "status": "parsed"}],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    snapshot = _snapshot()
    conventions = {"stale": False, "cards": [{"card_id": "card-naming", "card_kind": "naming", "status": "proposal", "statement": "snake_case", "confidence": 0.9, "freshness_score": 1.0, "evidence": {"path_hashes": {"src/service.py": "fixture-hash"}}}]}
    ready = build_change_impact_preview(snapshot, ["src/service.py"], conventions=conventions)
    stale = build_change_impact_preview(snapshot, ["src/service.py"], conventions={"stale": True, "cards": []})
    drift_rejected = False
    try:
        build_change_impact_preview(snapshot, ["src/service.py"], expected_graph_digest="different")
    except ChangeImpactError:
        drift_rejected = True
    stale_freshness = build_change_impact_preview(
        snapshot,
        ["src/service.py"],
        conventions={"stale": False, "cards": [{"card_id": "old", "card_kind": "naming", "status": "proposal", "statement": "snake_case", "confidence": 0.95, "freshness_score": 0.2, "evidence": {}}]},
    )
    timings: list[float] = []
    baseline_timings: list[float] = []
    for _ in range(25):
        started = time.perf_counter()
        build_change_impact_preview(snapshot, ["src/service.py"], conventions=conventions)
        timings.append((time.perf_counter() - started) * 1000.0)
        started = time.perf_counter()
        sorted({"src/service.py"})
        baseline_timings.append((time.perf_counter() - started) * 1000.0)
    timings.sort()
    baseline_timings.sort()
    def percentile(values: list[float], q: float) -> float:
        return values[min(len(values) - 1, max(0, int(round((len(values) - 1) * q))))]
    benchmark = {
        "iterations": len(timings),
        "preview_p50_ms": round(percentile(timings, 0.50), 4),
        "preview_p95_ms": round(percentile(timings, 0.95), 4),
        "baseline_p50_ms": round(percentile(baseline_timings, 0.50), 4),
        "delta_p50_ms": round(percentile(timings, 0.50) - percentile(baseline_timings, 0.50), 4),
        "baseline_definition": "repository-relative path normalization only; no graph traversal or convention selection",
    }
    repo_root = Path(__file__).resolve().parents[1]
    git_diff = collect_git_change_paths(repo_root)
    git_history = collect_git_history_stats(repo_root, git_diff["paths"][:8])
    report = {
        "schema_version": "bhm.p21.16.wi34.change-impact.v1",
        "generated_at": "2026-07-21",
        "plan_id": "BHM-V5-POST-ACCEPTANCE-20260717",
        "ready_case": ready,
        "stale_case_ready": stale["ready"],
        "stale_freshness_case_ready": stale_freshness["ready"],
        "digest_drift_rejected": drift_rejected,
        "git_diff": git_diff,
        "git_history": git_history,
        "decision_card_keys": sorted(ready["decision_card"]),
        "architecture_map": ready["architecture_map"],
        "benchmark": benchmark,
        "writes_live_state": False,
        "ok": bool(ready["ready"] and not stale["ready"] and not stale_freshness["ready"] and drift_rejected and ready["execution"]["auto_apply"] is False),
    }
    _write_report(args.report, report)
    print(json.dumps({"ok": report["ok"], "ready": ready["ready"], "stale_ready": stale["ready"], "digest_drift_rejected": drift_rejected}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
