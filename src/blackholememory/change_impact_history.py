"""Git-history and cross-repository change-impact correlations."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from .change_impact_base import (
    GIT_HISTORY_CORRELATION_SCHEMA_VERSION,
    MAX_CROSS_REPOSITORIES,
    MAX_CROSS_REPO_LINKS,
    MAX_HISTORY_RECEIPT_PATHS,
    MAX_DIFF_HUNKS,
    MAX_GIT_HISTORY_COMMITS,
    MAX_SYMBOL_CORRELATIONS,
    ChangeImpactError,
    _path,
    _sha256,
)


def _line_intersects(start: int, count: int, node_start: int, node_end: int) -> bool:
    """Return whether a bounded diff interval intersects a graph line span."""

    if count < 0 or node_start < 1 or node_end < node_start:
        return False
    # A zero-length insertion has no line to intersect.  Deletions still use
    # the old range and therefore normally have a positive old_count.
    if count == 0:
        return False
    end = start + count - 1
    return start <= node_end and node_start <= end


def correlate_diff_hunks_to_symbols(
    hunks: Sequence[Mapping[str, Any]],
    nodes: Sequence[Mapping[str, Any]],
    *,
    max_symbols: int = MAX_SYMBOL_CORRELATIONS,
) -> list[dict[str, Any]]:
    """Map diff line ranges to canonical graph symbol metadata.

    This is deliberately a pure metadata operation: it consumes line spans
    already published by the SQLite-authoritative graph and hunk coordinates
    emitted by Git, never source text or signatures.  Malformed/unbounded
    records fail closed by being ignored, and output ordering is canonical.
    """

    if not 1 <= int(max_symbols) <= MAX_SYMBOL_CORRELATIONS:
        raise ChangeImpactError("max_symbols must be between 1 and 128")
    normalized_hunks: list[dict[str, Any]] = []
    for item in list(hunks)[:MAX_DIFF_HUNKS]:
        try:
            path = _path(item.get("path"))
            values = {
                "path": path,
                "old_start": int(item.get("old_start")),
                "old_count": int(item.get("old_count") or 0),
                "new_start": int(item.get("new_start")),
                "new_count": int(item.get("new_count") or 0),
            }
        except (ChangeImpactError, TypeError, ValueError):
            continue
        if any(value < 0 for key, value in values.items() if key != "path"):
            continue
        if max(values["old_start"], values["new_start"]) > 2_000_000_000:
            continue
        normalized_hunks.append(values)
    by_key: dict[str, dict[str, Any]] = {}
    for node in list(nodes)[:100_000]:
        try:
            path = _path(node.get("path"))
            start_line = int(node.get("start_line"))
            end_line = int(node.get("end_line"))
        except (ChangeImpactError, TypeError, ValueError):
            continue
        if start_line < 1 or end_line < start_line or end_line > 2_000_000_000:
            continue
        stable_key = str(node.get("stable_key") or node.get("node_id") or "")
        if not stable_key:
            continue
        matches: list[dict[str, Any]] = []
        for hunk in normalized_hunks:
            if hunk["path"] != path:
                continue
            old_match = _line_intersects(hunk["old_start"], hunk["old_count"], start_line, end_line)
            new_match = _line_intersects(hunk["new_start"], hunk["new_count"], start_line, end_line)
            if not (old_match or new_match):
                continue
            matches.append(
                {
                    "old_start": hunk["old_start"],
                    "old_count": hunk["old_count"],
                    "new_start": hunk["new_start"],
                    "new_count": hunk["new_count"],
                    "match_scope": "both" if old_match and new_match else ("old" if old_match else "new"),
                }
            )
        if not matches:
            continue
        matches = sorted({json.dumps(item, sort_keys=True): item for item in matches}.values(), key=lambda item: (item["new_start"], item["old_start"], item["new_count"], item["old_count"]))
        by_key[stable_key] = {
            "stable_key": stable_key,
            "node_id": str(node.get("node_id") or ""),
            "path": path,
            "node_kind": str(node.get("node_kind") or ""),
            "name": str(node.get("name") or ""),
            "qualified_name": str(node.get("qualified_name") or ""),
            "language": str(node.get("language") or ""),
            "line_span": {"start": start_line, "end": end_line},
            "hunks": matches,
        }
    return sorted(
        by_key.values(),
        key=lambda item: (item["path"], item["line_span"]["start"], item["line_span"]["end"], item["node_kind"], item["qualified_name"], item["stable_key"]),
    )[: int(max_symbols)]


def correlate_git_history_to_symbols(
    history: Mapping[str, Any],
    nodes: Sequence[Mapping[str, Any]],
    *,
    max_symbols: int = MAX_SYMBOL_CORRELATIONS,
) -> list[dict[str, Any]]:
    """Attach bounded Git hotspot/co-change metadata to graph symbols."""

    if not 1 <= int(max_symbols) <= MAX_SYMBOL_CORRELATIONS:
        raise ChangeImpactError("max_symbols must be between 1 and 128")
    nodes_by_path: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for node in list(nodes)[:100_000]:
        try:
            path = _path(node.get("path"))
        except ChangeImpactError:
            continue
        stable_key = str(node.get("stable_key") or node.get("node_id") or "")
        if path and stable_key:
            nodes_by_path[path].append(node)
    correlations: list[dict[str, Any]] = []
    for item in list(history.get("hotspots") or [])[:32]:
        try:
            path = _path(item.get("path"))
            commits = int(item.get("commits") or 0)
        except (ChangeImpactError, TypeError, ValueError):
            continue
        for node in nodes_by_path.get(path, []):
            correlations.append({
                "relation": "hotspot",
                "path": path,
                "commits": max(0, commits),
                "stable_key": str(node.get("stable_key") or node.get("node_id") or ""),
                "node_id": str(node.get("node_id") or ""),
                "node_kind": str(node.get("node_kind") or ""),
                "name": str(node.get("name") or ""),
                "qualified_name": str(node.get("qualified_name") or ""),
                "language": str(node.get("language") or ""),
            })
    for item in list(history.get("cochange") or [])[:64]:
        try:
            changed_path = _path(item.get("changed_path"))
            companion_path = _path(item.get("companion_path"))
            commits = int(item.get("commits") or 0)
        except (ChangeImpactError, TypeError, ValueError):
            continue
        for node in nodes_by_path.get(companion_path, []):
            correlations.append({
                "relation": "cochange",
                "changed_path": changed_path,
                "companion_path": companion_path,
                "commits": max(0, commits),
                "stable_key": str(node.get("stable_key") or node.get("node_id") or ""),
                "node_id": str(node.get("node_id") or ""),
                "node_kind": str(node.get("node_kind") or ""),
                "name": str(node.get("name") or ""),
                "qualified_name": str(node.get("qualified_name") or ""),
                "language": str(node.get("language") or ""),
            })
    return sorted(
        correlations,
        key=lambda item: (
            item.get("relation", ""),
            -int(item.get("commits") or 0),
            item.get("path") or item.get("companion_path") or "",
            item.get("qualified_name") or "",
            item.get("stable_key") or "",
        ),
    )[: int(max_symbols)]


def build_git_history_correlation_receipt(
    history: Mapping[str, Any] | None,
    symbols: Sequence[Mapping[str, Any]] | None = None,
    *,
    changed_paths: Sequence[str] = (),
    max_commits: int = MAX_GIT_HISTORY_COMMITS,
) -> dict[str, Any]:
    """Build a bounded, deterministic receipt for Git-history correlation.

    The receipt deliberately consumes only the already-sanitized Git counters
    and graph symbol metadata.  It does not read Git, source text, signatures,
    or any store, and it never promotes a relationship.  A ``gap`` is honest
    when the requested paths have no history signal in the bounded window.
    """

    if not 1 <= int(max_commits) <= MAX_GIT_HISTORY_COMMITS:
        raise ChangeImpactError("max_commits must be between 1 and 64")
    source = history if isinstance(history, Mapping) else {}
    normalized_paths: list[str] = []
    for item in list(changed_paths)[:MAX_HISTORY_RECEIPT_PATHS]:
        if not str(item or "").strip():
            continue
        try:
            normalized_paths.append(_path(item))
        except ChangeImpactError:
            continue
    normalized_paths = sorted(set(normalized_paths))

    hotspots: list[dict[str, Any]] = []
    for item in list(source.get("hotspots") or [])[:32]:
        try:
            path = _path(item.get("path"))
            commits = max(0, int(item.get("commits") or 0))
        except (ChangeImpactError, TypeError, ValueError):
            continue
        hotspots.append({"path": path, "commits": commits})
    hotspots.sort(key=lambda item: (-int(item["commits"]), item["path"]))

    cochange: list[dict[str, Any]] = []
    for item in list(source.get("cochange") or [])[:64]:
        try:
            changed_path = _path(item.get("changed_path"))
            companion_path = _path(item.get("companion_path"))
            commits = max(0, int(item.get("commits") or 0))
        except (ChangeImpactError, TypeError, ValueError):
            continue
        cochange.append({"changed_path": changed_path, "companion_path": companion_path, "commits": commits})
    cochange.sort(key=lambda item: (-int(item["commits"]), item["changed_path"], item["companion_path"]))

    symbol_rows: list[dict[str, Any]] = []
    for item in list(symbols or [])[:MAX_SYMBOL_CORRELATIONS]:
        if not isinstance(item, Mapping):
            continue
        relation = str(item.get("relation") or "")
        if relation not in {"hotspot", "cochange"}:
            continue
        path_value = item.get("path") or item.get("companion_path")
        try:
            path = _path(path_value)
        except ChangeImpactError:
            continue
        stable_key = str(item.get("stable_key") or item.get("node_id") or "")[:300]
        if not stable_key:
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
        key=lambda item: (item["relation"], -int(item["commits"]), item["path"], item["qualified_name"], item["stable_key"]),
    )[:MAX_SYMBOL_CORRELATIONS]

    hotspot_map = {item["path"]: int(item["commits"]) for item in hotspots}
    companion_counts: Counter[str] = Counter()
    path_signals: list[dict[str, Any]] = []
    for path in normalized_paths:
        companions = [item for item in cochange if item["changed_path"] == path]
        for item in companions:
            companion_counts[item["companion_path"]] += int(item["commits"])
        matched_symbols = [item for item in symbol_rows if item["path"] == path]
        path_signals.append(
            {
                "path": path,
                "hotspot_commits": hotspot_map.get(path, 0),
                "cochange_companions": len(companions),
                "cochange_commits": sum(int(item["commits"]) for item in companions),
                "matched_symbols": len(matched_symbols),
            }
        )
    signal_paths = {item["path"] for item in hotspots} | {item["changed_path"] for item in cochange}
    observed_commits = max(0, min(int(source.get("commits_considered") or 0), int(max_commits)))
    gaps: list[str] = []
    if not normalized_paths:
        gaps.append("changed_paths_missing")
    if observed_commits == 0:
        gaps.append("git_history_missing")
    elif normalized_paths and not (set(normalized_paths) & signal_paths):
        gaps.append("changed_path_history_missing")
    status = "pass" if not gaps else "gap"
    core = {
        "schema_version": GIT_HISTORY_CORRELATION_SCHEMA_VERSION,
        "status": status,
        "gaps": sorted(set(gaps)),
        "changed_paths": normalized_paths,
        "history_window": {"commits_considered": observed_commits, "max_commits": int(max_commits)},
        "counts": {
            "hotspots": len(hotspots),
            "cochange_pairs": len(cochange),
            "correlated_symbols": len(symbol_rows),
            "signal_paths": len(signal_paths),
        },
        "path_signals": path_signals,
        "top_companions": [
            {"path": path, "weighted_commits": int(commits)}
            for path, commits in sorted(companion_counts.items(), key=lambda item: (-item[1], item[0]))[:32]
        ],
        "bounds": {
            "max_changed_paths": MAX_HISTORY_RECEIPT_PATHS,
            "max_history_commits": int(max_commits),
            "max_hotspots": 32,
            "max_cochange_pairs": 64,
            "max_symbol_correlations": MAX_SYMBOL_CORRELATIONS,
        },
    }
    return {
        **core,
        "receipt_digest": _sha256(core),
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


def build_cross_repo_history_preview(repositories: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Join bounded Git/history-symbol evidence across repositories.

    This is a proposal-only metadata join. It never reads source, executes
    Git itself, promotes graph edges or mutates any store; callers provide the
    already bounded per-repository evidence produced above.
    """

    items = list(repositories)[:MAX_CROSS_REPOSITORIES]
    if not items:
        raise ChangeImpactError("at least one repository evidence item is required")
    normalized: list[dict[str, Any]] = []
    for item in items:
        project = str(item.get("project") or "").strip()[:120]
        if not project:
            raise ChangeImpactError("repository project is required")
        history = item.get("history") if isinstance(item.get("history"), Mapping) else {}
        symbols = [symbol for symbol in list(item.get("symbols") or [])[:MAX_SYMBOL_CORRELATIONS] if isinstance(symbol, Mapping)]
        hotspot_paths = [entry.get("path") for entry in list(history.get("hotspots") or [])[:MAX_HISTORY_RECEIPT_PATHS] if isinstance(entry, Mapping)]
        normalized.append(
            {
                "project": project,
                "history": history,
                "symbols": symbols,
                "history_correlation": build_git_history_correlation_receipt(
                    history,
                    symbols,
                    changed_paths=hotspot_paths,
                ),
            }
        )
    normalized.sort(key=lambda item: item["project"])
    links: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for index, left in enumerate(normalized):
        left_symbols = {str(symbol.get("qualified_name") or ""): symbol for symbol in left["symbols"] if str(symbol.get("qualified_name") or "")}
        left_hotspots = {str(entry.get("path") or ""): int(entry.get("commits") or 0) for entry in list(left["history"].get("hotspots") or [])[:32]}
        for right in normalized[index + 1 :]:
            right_symbols = {str(symbol.get("qualified_name") or ""): symbol for symbol in right["symbols"] if str(symbol.get("qualified_name") or "")}
            for qualified_name in sorted(set(left_symbols) & set(right_symbols)):
                left_symbol = left_symbols[qualified_name]
                right_symbol = right_symbols[qualified_name]
                key = ("CROSS_GIT_SYMBOL", left["project"], right["project"], qualified_name)
                links[key] = {
                    "relation": "CROSS_GIT_SYMBOL",
                    "left_project": left["project"],
                    "right_project": right["project"],
                    "qualified_name": qualified_name[:300],
                    "left_stable_key": str(left_symbol.get("stable_key") or left_symbol.get("node_id") or "")[:300],
                    "right_stable_key": str(right_symbol.get("stable_key") or right_symbol.get("node_id") or "")[:300],
                    "evidence_class": "git-history-symbol-proposal",
                    "review_required": True,
                }
            right_hotspots = {str(entry.get("path") or ""): int(entry.get("commits") or 0) for entry in list(right["history"].get("hotspots") or [])[:32]}
            for left_path, left_commits in left_hotspots.items():
                left_name = PurePosixPath(left_path).name
                for right_path, right_commits in right_hotspots.items():
                    if left_name != PurePosixPath(right_path).name or not left_name:
                        continue
                    key = ("CROSS_GIT_HOTSPOT", left["project"], right["project"], left_name)
                    links[key] = {
                        "relation": "CROSS_GIT_HOTSPOT",
                        "left_project": left["project"],
                        "right_project": right["project"],
                        "left_path": left_path[:300],
                        "right_path": right_path[:300],
                        "left_commits": max(0, left_commits),
                        "right_commits": max(0, right_commits),
                        "evidence_class": "git-history-cohort-proposal",
                        "review_required": True,
                    }
    proposals = sorted(links.values(), key=lambda item: (item["relation"], item["left_project"], item["right_project"], item.get("qualified_name") or item.get("left_path") or ""))[:MAX_CROSS_REPO_LINKS]
    core = {
        "schema_version": "bhm.change-impact.cross-repo-history.v1",
        "repositories": [
            {
                "project": item["project"],
                "history_commits": int(item["history"].get("commits_considered") or 0),
                "symbol_count": len(item["symbols"]),
                "history_correlation": item["history_correlation"],
            }
            for item in normalized
        ],
        "proposals": proposals,
        "bounds": {"max_repositories": MAX_CROSS_REPOSITORIES, "max_links": MAX_CROSS_REPO_LINKS, "max_symbols_per_repository": MAX_SYMBOL_CORRELATIONS},
    }
    return {
        **core,
        "preview_digest": _sha256(core),
        "provenance": {"git_metadata_only": True, "graph_metadata_only": True, "authority": "proposal", "raw_source_returned": False},
        "execution": {"writes_worktree": False, "writes_sqlite_state": False, "writes_qdrant": False, "writes_mem0": False, "cross_edges_promoted": False, "auto_apply": False},
    }

__all__ = ["correlate_diff_hunks_to_symbols", "correlate_git_history_to_symbols", "build_git_history_correlation_receipt", "build_cross_repo_history_preview"]
