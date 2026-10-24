"""Deterministic change-impact, architecture-map and edit-preflight preview."""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from typing import Any, Mapping, Sequence

from .change_impact_base import (
    CHANGE_IMPACT_SCHEMA_VERSION,
    DIFF_HUNK_SUMMARY_SCHEMA_VERSION,
    GIT_HISTORY_CORRELATION_SCHEMA_VERSION,
    IMPACT_BINDING_SCHEMA_VERSION,
    IMPACT_PREVIEW_RESPONSE_BOUNDS_SCHEMA_VERSION,
    IMPACT_PREVIEW_RESPONSE_MAX_SERIALIZED_BYTES,
    MAX_CHANGED_PATHS,
    MAX_DIFF_HUNKS,
    MAX_GIT_HISTORY_COMMITS,
    MAX_SYMBOL_CORRELATIONS,
    ChangeImpactError,
    _git_context,
    _path,
    _sha256,
)
from .change_impact_history import (
    build_cross_repo_history_preview,
    build_git_history_correlation_receipt,
    correlate_diff_hunks_to_symbols,
    correlate_git_history_to_symbols,
)
from .change_impact_preview import (
    bound_change_impact_preview_response,
    build_change_impact_preview,
)
from .git_history_test_receipt import build_commit_symbol_test_history_receipt
from .resource_limits import PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS


def build_impact_binding_receipt(
    *,
    graph_snapshot_id: str | None,
    graph_digest: str | None,
    changed_paths: Sequence[str],
    diff_hunks: Sequence[Mapping[str, Any]],
    hunk_symbols: Sequence[Mapping[str, Any]],
    git_history: Mapping[str, Any] | None = None,
    expected_graph_digest: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Emit a deterministic, metadata-only public impact binding receipt.

    The receipt binds a proposal to one graph snapshot/digest and reports
    whether Git hunk and graph-symbol coverage are complete.  Missing
    caller-supplied evidence is a recoverable ``gap``; digest drift,
    out-of-scope paths, malformed coverage, source disclosure or mutation
    markers are hard ``fail`` states.  This helper consumes already captured
    metadata and never runs Git, reads source text, promotes edges or writes a
    store.
    """

    failures: list[str] = []
    gaps: list[str] = []
    snapshot_id = str(graph_snapshot_id or "").strip()
    bound_digest = str(graph_digest or "").strip()
    requested_digest = str(expected_graph_digest or "").strip()
    paths: list[str] = []
    for item in list(changed_paths)[:MAX_CHANGED_PATHS]:
        if not str(item or "").strip():
            continue
        try:
            paths.append(_path(item))
        except ChangeImpactError:
            failures.append("unsafe_changed_path")
    paths = sorted(set(paths))
    hunks = [item for item in list(diff_hunks)[: MAX_DIFF_HUNKS + 1] if isinstance(item, Mapping)]
    symbols = [item for item in list(hunk_symbols)[: MAX_SYMBOL_CORRELATIONS + 1] if isinstance(item, Mapping)]
    history = git_history if isinstance(git_history, Mapping) else {}
    provenance_data = provenance if isinstance(provenance, Mapping) else {}
    execution_data = execution if isinstance(execution, Mapping) else {}

    if not snapshot_id or not bound_digest:
        failures.append("graph_snapshot_binding_missing")
    if requested_digest:
        graph_digest_aligned = bool(bound_digest and bound_digest == requested_digest)
        if not graph_digest_aligned:
            failures.append("graph_digest_mismatch")
    else:
        graph_digest_aligned = False
        gaps.append("graph_digest_binding_missing")
    if not paths:
        failures.append("changed_paths_missing")
    if len(changed_paths) > MAX_CHANGED_PATHS:
        failures.append("changed_paths_cap_exceeded")
    if len(diff_hunks) > MAX_DIFF_HUNKS:
        failures.append("diff_hunks_cap_exceeded")
    if len(hunk_symbols) > MAX_SYMBOL_CORRELATIONS:
        failures.append("symbol_cap_exceeded")

    hunk_paths: set[str] = set()
    hunk_keys: set[tuple[str, int, int, int, int]] = set()
    for item in hunks:
        try:
            path = _path(item.get("path"))
            values = (path, int(item.get("old_start")), int(item.get("old_count") or 0), int(item.get("new_start")), int(item.get("new_count") or 0))
        except (ChangeImpactError, TypeError, ValueError):
            failures.append("malformed_diff_hunk")
            continue
        hunk_paths.add(path)
        hunk_keys.add(values)
    if hunk_paths and not hunk_paths.issubset(set(paths)):
        failures.append("hunk_path_outside_changed_paths")
    if not hunks:
        gaps.append("diff_hunk_coverage_missing")

    covered_hunks: set[tuple[str, int, int, int, int]] = set()
    for symbol in symbols:
        for item in list(symbol.get("hunks") or []):
            try:
                covered_hunks.add((_path(symbol.get("path")), int(item.get("old_start")), int(item.get("old_count") or 0), int(item.get("new_start")), int(item.get("new_count") or 0)))
            except (ChangeImpactError, TypeError, ValueError):
                failures.append("malformed_hunk_symbol")
    covered_hunks &= hunk_keys
    uncovered_hunks = sorted(hunk_keys - covered_hunks)
    if hunks and not symbols:
        gaps.append("hunk_symbol_coverage_missing")
    elif uncovered_hunks:
        gaps.append("hunk_symbol_coverage_incomplete")

    try:
        history_present = int(history.get("commits_considered") or 0) >= 1
    except (TypeError, ValueError):
        history_present = False
    if not history_present:
        gaps.append("git_history_missing")

    unsafe_execution = any(execution_data.get(key) is True for key in ("writes_sqlite_state", "writes_qdrant", "writes_worktree", "writes_mem0", "auto_apply", "edge_promotion", "cross_edges_promoted"))
    if unsafe_execution:
        failures.append("proposal_only_execution_boundary_failed")
    metadata_only = provenance_data.get("raw_source_returned") is False and provenance_data.get("graph_metadata_only") is True and provenance_data.get("git_metadata_only") is True
    if not metadata_only:
        failures.append("metadata_only_provenance_failed")

    checks = {
        "graph_snapshot_bound": bool(snapshot_id and bound_digest),
        "graph_digest_aligned": graph_digest_aligned,
        "changed_paths_bounded": bool(paths) and len(changed_paths) <= MAX_CHANGED_PATHS,
        "hunk_paths_covered": bool(hunk_paths) and hunk_paths.issubset(set(paths)),
        "hunk_symbol_coverage": bool(hunk_keys) and not uncovered_hunks,
        "history_present": history_present,
        "proposal_only": not unsafe_execution,
        "metadata_only": metadata_only,
    }
    status = "fail" if failures else ("gap" if gaps else "pass")
    receipt = {
        "schema_version": IMPACT_BINDING_SCHEMA_VERSION,
        "status": status,
        "ok": status == "pass",
        "graph_binding": {"graph_snapshot_id": snapshot_id, "graph_digest": bound_digest, "expected_graph_digest": requested_digest, "aligned": graph_digest_aligned},
        "coverage": {
            "changed_paths": len(paths),
            "diff_hunks": len(hunks),
            "hunk_symbols": len(symbols),
            "hunks_with_symbol": len(covered_hunks),
            "uncovered_hunks": len(uncovered_hunks),
            "complete": bool(hunk_keys) and not uncovered_hunks,
            "caps": {"changed_paths": MAX_CHANGED_PATHS, "diff_hunks": MAX_DIFF_HUNKS, "symbols": MAX_SYMBOL_CORRELATIONS},
        },
        "diff_summary": summarize_diff_hunks(diff_hunks),
        "checks": checks,
        "gaps": sorted(set(gaps)),
        "failures": sorted(set(failures)),
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_mem0": False, "writes_worktree": False, "auto_apply": False, "edge_promotion": False, "raw_source_returned": False},
    }
    receipt["receipt_digest"] = _sha256({key: value for key, value in receipt.items() if key != "receipt_digest"})
    return receipt


def collect_git_change_paths(repo_root: str | os.PathLike[str], *, base_revision: str | None = None) -> dict[str, Any]:
    """Read a bounded git diff/status path set without changing the worktree."""

    root, environment = _git_context(repo_root)
    command = ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB"]
    if base_revision:
        revision = str(base_revision).strip()
        if not (7 <= len(revision) <= 64 and all(char in "0123456789abcdefABCDEF" for char in revision)):
            raise ChangeImpactError("base_revision must be a hexadecimal git revision")
        command.extend([revision, "--"])
    else:
        command.append("--")
    completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", cwd=root, env=environment, timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], check=True, capture_output=True, text=True, encoding="utf-8", cwd=root, env=environment, timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS)
    candidates = [line for line in completed.stdout.splitlines() if line.strip()] + [line for line in untracked.stdout.splitlines() if line.strip()]
    paths = sorted({_path(line) for line in candidates})[:MAX_CHANGED_PATHS]
    return {"paths": paths, "base_revision": base_revision, "untracked_included": True, "bounded": len(paths) <= MAX_CHANGED_PATHS, "diff_hunks": collect_git_diff_hunks(root, base_revision=base_revision, paths=paths) if base_revision else [], "writes_worktree": False}


def collect_git_diff_hunks(
    repo_root: str | os.PathLike[str],
    *,
    base_revision: str,
    paths: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return bounded line-range metadata from git diff, never diff text."""

    revision = str(base_revision or "").strip()
    if not (7 <= len(revision) <= 64 and all(char in "0123456789abcdefABCDEF" for char in revision)):
        raise ChangeImpactError("base_revision must be a hexadecimal git revision")
    root, environment = _git_context(repo_root)
    selected = sorted({_path(path) for path in (paths or []) if str(path or "").strip()})[:MAX_CHANGED_PATHS]
    # Do not place caller-supplied paths in the subprocess argv.  Git emits a
    # bounded metadata diff for the repository, and the already-normalized
    # allowlist is applied while parsing the output below.
    command = ["git", "diff", "--unified=0", revision, "--"]
    # lgtm [py/command-line-injection]
    completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", cwd=root, env=environment, timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS)
    current_path = ""
    selected_set = set(selected)
    hunks: list[dict[str, Any]] = []
    hunk_pattern = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")
    for line in completed.stdout.splitlines():
        if line.startswith("+++ b/"):
            try:
                current_path = _path(line[6:])
            except ChangeImpactError:
                current_path = ""
            if selected_set and current_path not in selected_set:
                current_path = ""
            continue
        match = hunk_pattern.match(line)
        if not match or not current_path:
            continue
        hunks.append(
            {
                "path": current_path,
                "old_start": int(match.group("old_start")),
                "old_count": int(match.group("old_count") or 1),
                "new_start": int(match.group("new_start")),
                "new_count": int(match.group("new_count") or 1),
            }
        )
        if len(hunks) >= MAX_DIFF_HUNKS:
            break
    return hunks


def summarize_diff_hunks(hunks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize bounded Git hunk geometry without exposing diff text.

    The summary is intentionally proposal-only metadata: it counts additions,
    deletions and replacements from caller-supplied line ranges.  Malformed
    records are skipped and reported by count; no source text, Git command or
    store access is performed here.
    """

    raw = list(hunks)
    normalized: list[dict[str, Any]] = []
    invalid = 0
    for item in raw[: MAX_DIFF_HUNKS + 1]:
        if not isinstance(item, Mapping):
            invalid += 1
            continue
        try:
            path = _path(item.get("path"))
            old_count = int(item.get("old_count") or 0)
            new_count = int(item.get("new_count") or 0)
        except (ChangeImpactError, TypeError, ValueError):
            invalid += 1
            continue
        if old_count < 0 or new_count < 0:
            invalid += 1
            continue
        if old_count == 0 and new_count == 0:
            change_kind = "empty"
        elif old_count == 0:
            change_kind = "insert"
        elif new_count == 0:
            change_kind = "delete"
        else:
            change_kind = "replace"
        normalized.append({"path": path, "old_count": old_count, "new_count": new_count, "change_kind": change_kind})

    by_path: dict[str, dict[str, Any]] = {}
    kind_counts: Counter[str] = Counter()
    for item in normalized[:MAX_DIFF_HUNKS]:
        path = item["path"]
        entry = by_path.setdefault(path, {"path": path, "hunks": 0, "added_lines": 0, "removed_lines": 0, "net_lines": 0, "change_kinds": {"insert": 0, "delete": 0, "replace": 0, "empty": 0}})
        entry["hunks"] += 1
        entry["added_lines"] += item["new_count"]
        entry["removed_lines"] += item["old_count"]
        entry["net_lines"] += item["new_count"] - item["old_count"]
        entry["change_kinds"][item["change_kind"]] += 1
        kind_counts[item["change_kind"]] += 1

    files = sorted(by_path.values(), key=lambda item: item["path"])
    totals = {
        "files": len(files),
        "hunks": sum(int(item["hunks"]) for item in files),
        "added_lines": sum(int(item["added_lines"]) for item in files),
        "removed_lines": sum(int(item["removed_lines"]) for item in files),
        "net_lines": sum(int(item["net_lines"]) for item in files),
        "change_kinds": {key: int(kind_counts.get(key, 0)) for key in ("insert", "delete", "replace", "empty")},
    }
    core = {
        "schema_version": DIFF_HUNK_SUMMARY_SCHEMA_VERSION,
        "totals": totals,
        "files": files,
        "invalid_hunks": invalid,
        "truncated": len(raw) > MAX_DIFF_HUNKS,
        "caps": {"diff_hunks": MAX_DIFF_HUNKS},
    }
    return {**core, "summary_digest": _sha256(core)}


def collect_git_history_stats(repo_root: str | os.PathLike[str], changed_paths: Sequence[str], *, max_commits: int = MAX_GIT_HISTORY_COMMITS) -> dict[str, Any]:
    """Return bounded co-change/hotspot counts from git metadata only."""

    if not 1 <= int(max_commits) <= MAX_GIT_HISTORY_COMMITS:
        raise ChangeImpactError("max_commits must be between 1 and 64")
    root, environment = _git_context(repo_root)
    completed = subprocess.run(["git", "log", f"--max-count={int(max_commits)}", "--format=%H", "--name-only", "--diff-filter=ACMRTUXB", "--"], check=True, capture_output=True, text=True, encoding="utf-8", cwd=root, env=environment, timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS)
    commits: list[list[str]] = []
    commit_ids: list[str] = []
    current: list[str] = []
    current_id = ""
    for line in completed.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        if len(value) == 40 and all(char in "0123456789abcdefABCDEF" for char in value):
            if current:
                commits.append(current)
                commit_ids.append(current_id)
            current = []
            current_id = value.casefold()
            continue
        try:
            current.append(_path(value))
        except ChangeImpactError:
            continue
    if current:
        commits.append(current)
        commit_ids.append(current_id)
    wanted = {_path(path) for path in changed_paths}
    hotspot: Counter[str] = Counter()
    cochange: Counter[tuple[str, str]] = Counter()
    for files in commits[: int(max_commits)]:
        unique = sorted(set(files))
        for path in unique:
            hotspot[path] += 1
        touched = sorted(wanted & set(unique))
        companions = sorted(set(unique) - wanted)
        for source in touched:
            for companion in companions[:64]:
                cochange[(source, companion)] += 1
    bounded_commits = list(zip(commit_ids, commits))[: int(max_commits)]
    commit_records = [
        {
            "commit_digest": _sha256(commit_id)[:32] if commit_id else "",
            "file_count": len(sorted(set(files))[:64]),
            "paths": sorted(set(files))[:64],
            "touches_changed_paths": bool(wanted & set(files)),
        }
        for commit_id, files in bounded_commits
    ]
    return {"commits_considered": min(len(commits), int(max_commits)), "hotspots": [{"path": path, "commits": count} for path, count in sorted(hotspot.items(), key=lambda item: (-item[1], item[0]))[:32]], "cochange": [{"changed_path": source, "companion_path": companion, "commits": count} for (source, companion), count in sorted(cochange.items(), key=lambda item: (-item[1], item[0]))[:64]], "commit_records": commit_records, "writes_worktree": False}


def build_git_symbol_impact_evidence(
    repo_root: str | os.PathLike[str],
    changed_paths: Sequence[str],
    nodes: Sequence[Mapping[str, Any]],
    *,
    base_revision: str | None = None,
    max_commits: int = MAX_GIT_HISTORY_COMMITS,
) -> dict[str, Any]:
    """Build deterministic Git-to-symbol evidence without source persistence."""

    paths = sorted({_path(item) for item in list(changed_paths)[:MAX_CHANGED_PATHS] if str(item or "").strip()})
    hunks = collect_git_diff_hunks(repo_root, base_revision=base_revision, paths=paths) if base_revision else []
    history = collect_git_history_stats(repo_root, paths, max_commits=max_commits)
    core = {
        "schema_version": "bhm.change-impact.git-symbols.v1",
        "changed_paths": paths,
        "base_revision": str(base_revision or ""),
        "diff_hunks": hunks,
        "diff_summary": summarize_diff_hunks(hunks),
        "hunk_symbols": correlate_diff_hunks_to_symbols(hunks, nodes),
        "git_history": history,
        "history_symbols": correlate_git_history_to_symbols(history, nodes),
        "bounds": {"max_changed_paths": MAX_CHANGED_PATHS, "max_diff_hunks": MAX_DIFF_HUNKS, "max_history_commits": max_commits, "max_symbol_correlations": MAX_SYMBOL_CORRELATIONS},
    }
    core["history_correlation"] = build_git_history_correlation_receipt(
        history,
        core["history_symbols"],
        changed_paths=paths,
        max_commits=max_commits,
    )
    core["commit_symbol_test_history"] = build_commit_symbol_test_history_receipt(
        history,
        core["history_symbols"],
        nodes,
        changed_paths=paths,
        max_commits=max_commits,
    )
    return {
        **core,
        "evidence_digest": _sha256(core),
        "provenance": {"git_metadata_only": True, "graph_metadata_only": True, "raw_source_returned": False},
        "execution": {"writes_worktree": False, "writes_sqlite_state": False, "writes_qdrant": False, "writes_mem0": False, "auto_apply": False, "authority": "proposal-only"},
    }


__all__ = ["CHANGE_IMPACT_SCHEMA_VERSION", "IMPACT_BINDING_SCHEMA_VERSION", "IMPACT_PREVIEW_RESPONSE_BOUNDS_SCHEMA_VERSION", "IMPACT_PREVIEW_RESPONSE_MAX_SERIALIZED_BYTES", "DIFF_HUNK_SUMMARY_SCHEMA_VERSION", "GIT_HISTORY_CORRELATION_SCHEMA_VERSION", "ChangeImpactError", "bound_change_impact_preview_response", "build_change_impact_preview", "build_impact_binding_receipt", "build_cross_repo_history_preview", "build_git_history_correlation_receipt", "build_git_symbol_impact_evidence", "collect_git_change_paths", "collect_git_diff_hunks", "collect_git_history_stats", "correlate_diff_hunks_to_symbols", "correlate_git_history_to_symbols", "summarize_diff_hunks"]
