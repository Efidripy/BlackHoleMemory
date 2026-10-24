"""Bounded commit-to-symbol-to-test history evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


COMMIT_SYMBOL_TEST_HISTORY_SCHEMA_VERSION = "bhm.change-impact.commit-symbol-test-history-receipt.v1"
MAX_CHANGED_PATHS = 64
MAX_COMMITS = 64
MAX_SYMBOLS = 128
MAX_TESTS = 64


class GitHistoryTestReceiptError(ValueError):
    """Raised when bounded commit/symbol/test metadata is malformed."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    normalized = str(PurePosixPath(raw)) if raw else ""
    if not normalized or normalized in {".", ".."} or normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        raise GitHistoryTestReceiptError("repository-relative path required")
    return normalized


def _is_test_node(node: Mapping[str, Any]) -> bool:
    node_kind = str(node.get("node_kind") or node.get("kind") or "").casefold()
    try:
        path = _path(node.get("path"))
    except GitHistoryTestReceiptError:
        path = ""
    return node_kind == "test" or path == "tests" or path.startswith("tests/") or "/tests/" in path


def build_commit_symbol_test_history_receipt(
    history: Mapping[str, Any] | None,
    symbols: Sequence[Mapping[str, Any]] | None = None,
    tests: Sequence[Mapping[str, Any]] | None = None,
    *,
    changed_paths: Sequence[str] = (),
    max_commits: int = MAX_COMMITS,
) -> dict[str, Any]:
    """Build deterministic metadata-only commit/symbol/test history evidence.

    Inputs are already sanitized Git counters and SQLite graph metadata.  No
    source text, commit messages, signatures, worktree or store is read here.
    Relationships remain proposal-only and are never promoted to the graph.
    """

    if not 1 <= int(max_commits) <= MAX_COMMITS:
        raise GitHistoryTestReceiptError("max_commits must be between 1 and 64")
    source = history if isinstance(history, Mapping) else {}
    excepted_paths: list[str] = []
    # Re-normalize fail-closed without leaking malformed caller values.
    safe_paths: list[str] = []
    for item in list(changed_paths)[:MAX_CHANGED_PATHS]:
        try:
            if str(item or "").strip():
                safe_paths.append(_path(item))
        except GitHistoryTestReceiptError:
            excepted_paths.append("invalid")
    normalized_paths = sorted(set(safe_paths))

    hotspots: list[dict[str, Any]] = []
    for item in list(source.get("hotspots") or [])[:32]:
        if not isinstance(item, Mapping):
            continue
        try:
            hotspots.append({"path": _path(item.get("path")), "commits": max(0, int(item.get("commits") or 0))})
        except (GitHistoryTestReceiptError, TypeError, ValueError):
            continue
    cochange: list[dict[str, Any]] = []
    for item in list(source.get("cochange") or [])[:64]:
        if not isinstance(item, Mapping):
            continue
        try:
            cochange.append(
                {
                    "changed_path": _path(item.get("changed_path")),
                    "companion_path": _path(item.get("companion_path")),
                    "commits": max(0, int(item.get("commits") or 0)),
                }
            )
        except (GitHistoryTestReceiptError, TypeError, ValueError):
            continue
    hotspots.sort(key=lambda item: (-item["commits"], item["path"]))
    cochange.sort(key=lambda item: (-item["commits"], item["changed_path"], item["companion_path"]))
    history_paths = {item["path"] for item in hotspots}
    history_paths.update(item["companion_path"] for item in cochange)
    observed_commits = max(0, min(int(source.get("commits_considered") or 0), int(max_commits)))
    commit_records: list[dict[str, Any]] = []
    for item in list(source.get("commit_records") or [])[: int(max_commits)]:
        if not isinstance(item, Mapping):
            continue
        digest = str(item.get("commit_digest") or "")[:64]
        if not digest:
            continue
        safe_record_paths: list[str] = []
        for value in list(item.get("paths") or [])[:64]:
            try:
                safe_record_paths.append(_path(value))
            except GitHistoryTestReceiptError:
                continue
        commit_records.append(
            {
                "commit_digest": digest,
                "file_count": max(0, min(int(item.get("file_count") or len(safe_record_paths)), 64)),
                "paths": sorted(set(safe_record_paths)),
                "touches_changed_paths": bool(item.get("touches_changed_paths")),
            }
        )

    symbol_rows: list[dict[str, Any]] = []
    for item in list(symbols or [])[:MAX_SYMBOLS]:
        if not isinstance(item, Mapping):
            continue
        try:
            path = _path(item.get("path") or item.get("companion_path"))
        except GitHistoryTestReceiptError:
            continue
        stable_key = str(item.get("stable_key") or item.get("node_id") or "")[:300]
        if not stable_key:
            continue
        relation = str(item.get("relation") or "")
        if relation not in {"hotspot", "cochange"}:
            continue
        try:
            commits = max(0, int(item.get("commits") or 0))
        except (TypeError, ValueError):
            commits = 0
        symbol_rows.append(
            {
                "relation": relation,
                "path": path,
                "stable_key": stable_key,
                "node_kind": str(item.get("node_kind") or "")[:80],
                "qualified_name": str(item.get("qualified_name") or "")[:300],
                "commits": commits,
            }
        )
    symbol_rows = sorted(
        {json.dumps(item, sort_keys=True): item for item in symbol_rows}.values(),
        key=lambda item: (item["relation"], -item["commits"], item["path"], item["qualified_name"], item["stable_key"]),
    )[:MAX_SYMBOLS]
    symbol_paths = {item["path"] for item in symbol_rows}
    test_rows: list[dict[str, Any]] = []
    for node in list(tests or [])[:100_000]:
        if not isinstance(node, Mapping) or not _is_test_node(node):
            continue
        try:
            path = _path(node.get("path"))
        except GitHistoryTestReceiptError:
            continue
        if path not in history_paths:
            continue
        stable_key = str(node.get("stable_key") or node.get("node_id") or "")[:300]
        if not stable_key:
            continue
        hotspot_commits = max((item["commits"] for item in hotspots if item["path"] == path), default=0)
        cochange_commits = sum(item["commits"] for item in cochange if item["companion_path"] == path)
        relation = "hotspot" if hotspot_commits >= cochange_commits and hotspot_commits else "cochange"
        test_rows.append(
            {
                "path": path,
                "stable_key": stable_key,
                "node_kind": str(node.get("node_kind") or node.get("kind") or "test")[:80],
                "qualified_name": str(node.get("qualified_name") or node.get("name") or "")[:300],
                "relation": relation,
                "commits": max(hotspot_commits, cochange_commits),
            }
        )
    test_rows = list({json.dumps(item, sort_keys=True): item for item in test_rows}.values())
    test_rows.sort(key=lambda item: (-item["commits"], item["path"], item["qualified_name"], item["stable_key"]))
    test_rows = test_rows[:MAX_TESTS]
    test_paths = {item["path"] for item in test_rows}
    commit_links: list[dict[str, Any]] = []
    for item in commit_records:
        paths = set(item["paths"])
        commit_links.append(
            {
                "commit_digest": item["commit_digest"],
                "touches_changed_paths": item["touches_changed_paths"],
                "symbol_paths": sorted(paths & symbol_paths)[:MAX_SYMBOLS],
                "test_paths": sorted(paths & test_paths)[:MAX_TESTS],
            }
        )

    gaps: list[str] = []
    if observed_commits == 0:
        gaps.append("git_history_missing")
    if not symbol_rows:
        gaps.append("symbol_history_missing")
    if not test_rows:
        gaps.append("test_history_missing")
    if observed_commits and not commit_records:
        gaps.append("commit_records_missing")
    if excepted_paths:
        gaps.append("invalid_changed_paths_ignored")
    core = {
        "schema_version": COMMIT_SYMBOL_TEST_HISTORY_SCHEMA_VERSION,
        "status": "pass" if not gaps else "gap",
        "gaps": sorted(set(gaps)),
        "changed_paths": normalized_paths,
        "history_window": {"commits_considered": observed_commits, "max_commits": int(max_commits)},
        "counts": {
            "hotspots": len(hotspots),
            "cochange_pairs": len(cochange),
            "symbol_correlations": len(symbol_rows),
            "test_correlations": len(test_rows),
            "commit_records": len(commit_records),
            "commit_symbol_links": sum(bool(item["symbol_paths"]) for item in commit_links),
            "commit_test_links": sum(bool(item["test_paths"]) for item in commit_links),
            "history_paths": len(history_paths),
        },
        "commit_records": commit_links,
        "symbol_correlations": symbol_rows,
        "test_correlations": test_rows,
        "bounds": {
            "max_changed_paths": MAX_CHANGED_PATHS,
            "max_history_commits": int(max_commits),
            "max_symbol_correlations": MAX_SYMBOLS,
            "max_test_correlations": MAX_TESTS,
        },
    }
    return {
        **core,
        "receipt_digest": _digest(core),
        "provenance": {"git_metadata_only": True, "graph_metadata_only": True, "raw_source_returned": False},
        "execution": {
            "writes_worktree": False,
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_mem0": False,
            "cross_edges_promoted": False,
            "auto_apply": False,
            "authority": "proposal-only",
        },
    }


__all__ = ["COMMIT_SYMBOL_TEST_HISTORY_SCHEMA_VERSION", "GitHistoryTestReceiptError", "build_commit_symbol_test_history_receipt"]
