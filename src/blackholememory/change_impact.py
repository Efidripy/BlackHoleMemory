"""Deterministic change-impact, architecture-map and edit-preflight preview."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import Counter, defaultdict, deque
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .git_history_test_receipt import build_commit_symbol_test_history_receipt


CHANGE_IMPACT_SCHEMA_VERSION = "bhm.change-impact.v1"
IMPACT_BINDING_SCHEMA_VERSION = "bhm.change-impact.binding.v1"
MAX_CHANGED_PATHS = 64
MAX_IMPACT_NODES = 128
MAX_TEST_PATHS = 32
MAX_GIT_HISTORY_COMMITS = 64
MAX_DIFF_HUNKS = 128
MAX_SYMBOL_CORRELATIONS = 128
MAX_CROSS_REPOSITORIES = 8
MAX_CROSS_REPO_LINKS = 64
MIN_CONFIDENCE = 0.65
IMPACT_EDGE_KINDS = frozenset({"calls", "async_calls", "imports", "inherits", "tests", "route_handles", "contains", "http_calls", "emits", "listens_on", "data_flows", "depends_on", "exposes"})
DIFF_HUNK_SUMMARY_SCHEMA_VERSION = "bhm.change-impact.hunk-summary.v1"
GIT_HISTORY_CORRELATION_SCHEMA_VERSION = "bhm.change-impact.git-history-correlation.v1"
MAX_HISTORY_RECEIPT_PATHS = 64


class ChangeImpactError(ValueError):
    """Raised when an edit-preflight request cannot be safely evaluated."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    normalized = str(PurePosixPath(raw)) if raw else ""
    if not normalized or normalized in {".", ".."} or normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        raise ChangeImpactError("changed path must be repository-relative")
    return normalized


def _git_context(repo_root: str | os.PathLike[str]) -> tuple[Path, dict[str, str]]:
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        raise ChangeImpactError("repository root must be a directory")
    environment = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(root),
    }
    return root, environment


def _node_key(node: Mapping[str, Any]) -> str:
    return str(node.get("node_id") or node.get("stable_key") or "")


def _architecture_map(nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    components: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    routes: list[str] = []
    services: Counter[str] = Counter()
    packages: Counter[str] = Counter()
    service_hints: Counter[str] = Counter()
    package_hints: Counter[str] = Counter()
    for node in nodes:
        path = str(node.get("path") or "").replace("\\", "/")
        if path:
            parts = PurePosixPath(path).parts
            components[parts[0] if len(parts) > 1 else "(root)"] += 1
        language = str(node.get("language") or "")
        if language:
            languages[language] += 1
        if node.get("node_kind") == "route":
            routes.append(str(node.get("qualified_name") or node.get("name") or ""))
        if node.get("node_kind") in {"service", "repository", "database"}:
            services[str(node.get("name") or node.get("qualified_name") or "(unnamed)")] += 1
        if path and PurePosixPath(path).name.casefold() in {"pyproject.toml", "package.json", "package-lock.json", "uv.lock", "requirements.txt", "dockerfile", "compose.yaml", "compose.yml"}:
            packages[PurePosixPath(path).name] += 1
        parts_lower = {part.casefold() for part in PurePosixPath(path).parts}
        for marker in ("service", "services", "worker", "workers", "api", "runtime"):
            if marker in parts_lower:
                service_hints[marker] += 1
        filename = PurePosixPath(path).name.casefold()
        if filename in {"pyproject.toml", "package.json", "package-lock.json", "uv.lock", "requirements.txt", "poetry.lock", "dockerfile", "compose.yaml", "compose.yml", "go.mod", "cargo.toml"}:
            package_hints[filename] += 1
    return {
        "components": dict(sorted(components.items())),
        "languages": dict(sorted(languages.items())),
        "routes": sorted(route for route in routes if route),
        "services": dict(sorted(services.items())),
        "packages": dict(sorted(packages.items())),
        "service_hints": dict(sorted(service_hints.items())),
        "package_hints": dict(sorted(package_hints.items())),
        "edge_kinds": dict(sorted(Counter(str(edge.get("edge_kind") or "") for edge in edges).items())),
    }


def _patterns(conventions: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    cards = list((conventions or {}).get("cards") or [])
    stale = bool((conventions or {}).get("stale"))
    patterns: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    seen_kinds: dict[str, dict[str, Any]] = {}
    for card in cards:
        kind = str(card.get("card_kind") or "")
        if not kind:
            continue
        status = str(card.get("status") or "proposal")
        if status == "rejected":
            conflicts.append({"card_id": card.get("card_id"), "kind": kind, "reason": "rejected convention"})
            continue
        if kind in seen_kinds and seen_kinds[kind].get("statement") != card.get("statement"):
            conflicts.append({"card_id": card.get("card_id"), "kind": kind, "reason": "conflicting statements"})
            continue
        normalized = {
            "card_id": card.get("card_id"),
            "kind": kind,
            "statement": str(card.get("statement") or "")[:1_500],
            "confidence": float(card.get("confidence") or 0.0),
            "freshness_score": float(card.get("freshness_score") or 0.0),
            "evidence": card.get("evidence") or {},
        }
        seen_kinds[kind] = normalized
        patterns.append(normalized)
    patterns.sort(key=lambda item: (-item["confidence"], item["kind"], str(item.get("card_id") or "")))
    low_evidence = bool(patterns) and any(
        float(item.get("confidence") or 0.0) < MIN_CONFIDENCE
        or float(item.get("freshness_score") or 0.0) < MIN_CONFIDENCE
        for item in patterns
    )
    return patterns, conflicts, stale, low_evidence


def build_change_impact_preview(
    snapshot: Mapping[str, Any],
    changed_paths: Sequence[str],
    *,
    conventions: Mapping[str, Any] | None = None,
    expected_graph_digest: str | None = None,
    min_confidence: float = MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Build a bounded proposal; no source text or writes are returned/performed."""

    graph_snapshot_id = str(snapshot.get("graph_snapshot_id") or snapshot.get("snapshot_id") or "")
    graph_digest = str(snapshot.get("graph_digest") or "")
    if not graph_snapshot_id or not graph_digest:
        raise ChangeImpactError("completed graph snapshot with digest is required")
    if expected_graph_digest and expected_graph_digest != graph_digest:
        raise ChangeImpactError("graph digest changed; refresh index and graph before edit preflight")
    current_graph_digest = str(snapshot.get("current_graph_digest") or "")
    graph_stale = bool(snapshot.get("stale") or (current_graph_digest and current_graph_digest != graph_digest))
    paths = sorted({_path(item) for item in list(changed_paths)[:MAX_CHANGED_PATHS] if str(item or "").strip()})
    nodes = list(snapshot.get("nodes") or [])
    edges = list(snapshot.get("edges") or [])
    parse_results = list(snapshot.get("parse_results") or [])
    parse_errors = [item for item in parse_results if str(item.get("status")) == "error"]
    node_by_id = {_node_key(node): node for node in nodes if _node_key(node)}
    path_nodes = [node for node in nodes if str(node.get("path") or "").replace("\\", "/") in paths]
    path_node_ids = {_node_key(node) for node in path_nodes}
    adjacency: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for edge in edges:
        if str(edge.get("edge_kind") or "") not in IMPACT_EDGE_KINDS:
            continue
        source = str(edge.get("source_node_id") or "")
        target = str(edge.get("target_node_id") or "")
        if source and target:
            adjacency[source].append(edge)
            adjacency[target].append({**edge, "source_node_id": target, "target_node_id": source})
    impacted_ids = set(path_node_ids)
    queue: deque[tuple[str, int]] = deque((item, 0) for item in sorted(path_node_ids))
    while queue and len(impacted_ids) < MAX_IMPACT_NODES:
        current, depth = queue.popleft()
        if depth >= 3:
            continue
        for edge in sorted(adjacency.get(current, []), key=lambda item: str(item.get("stable_key") or "")):
            target = str(edge.get("target_node_id") or "")
            if target in node_by_id and target not in impacted_ids:
                impacted_ids.add(target)
                queue.append((target, depth + 1))
                if len(impacted_ids) >= MAX_IMPACT_NODES:
                    break
    impacted_nodes = [node_by_id[item] for item in sorted(impacted_ids) if item in node_by_id]
    selected_tests = sorted(
        {
            str(node.get("path") or "")
            for node in impacted_nodes
            if node.get("node_kind") == "test" or str(node.get("path") or "").replace("\\", "/").startswith("tests/")
        }
    )[:MAX_TEST_PATHS]
    patterns, conflicts, conventions_stale, low_evidence = _patterns(conventions)
    best_examples = sorted(
        [example for card in patterns for example in list((card.get("evidence") or {}).get("path_hashes", {}).keys())],
    )[:8]
    covered_paths = {str(node.get("path") or "").replace("\\", "/") for node in path_nodes}
    coverage = bool(paths) and set(paths).issubset(covered_paths) and not parse_errors
    low_confidence = bool(patterns) and any(
        float(item.get("confidence") or 0.0) < float(min_confidence)
        or float(item.get("freshness_score") or 0.0) < float(min_confidence)
        for item in patterns
    )
    confidence = round(min(1.0, (0.85 if coverage else 0.25) * (0.75 if (conventions_stale or graph_stale) else 1.0) * (0.75 if conflicts else 1.0) * (0.75 if low_confidence else 1.0)), 4)
    ready = bool(coverage and not conventions_stale and not graph_stale and not conflicts and not low_confidence and confidence >= float(min_confidence))
    what_would_help: list[str] = []
    if not paths:
        what_would_help.append("provide at least one repository-relative changed path")
    if not path_nodes:
        what_would_help.append("refresh WI-01/WI-02 index and graph for changed paths")
    elif not set(paths).issubset(covered_paths):
        what_would_help.append("index every changed path before edit preflight")
    if parse_errors:
        what_would_help.append("resolve parser errors before edit preflight")
    if conventions_stale:
        what_would_help.append("rebuild convention cards from the current graph")
    if graph_stale:
        what_would_help.append("refresh the graph because the current graph digest differs")
    if conflicts:
        what_would_help.append("review conflicting or rejected convention cards")
    if low_confidence:
        what_would_help.append("review low-confidence or stale convention evidence")
    core = {
        "project": str(snapshot.get("project") or "blackholememory"),
        "graph_snapshot_id": graph_snapshot_id,
        "graph_digest": graph_digest,
        "changed_paths": paths,
        "architecture_map": _architecture_map(impacted_nodes, edges),
        "impact": [{"path": str(node.get("path") or ""), "node_kind": node.get("node_kind"), "name": node.get("name"), "language": node.get("language"), "stable_key": node.get("stable_key")} for node in impacted_nodes[:MAX_IMPACT_NODES]],
        "patterns": patterns[:16],
        "bestExample": best_examples,
        "selectedTests": selected_tests,
        "whatWouldHelp": what_would_help,
        "conflicts": conflicts,
        "confidence": confidence,
        "stale": bool(conventions_stale or graph_stale),
        "graph_stale": graph_stale,
        "low_confidence": low_confidence,
        "low_evidence": low_evidence,
        "ready": ready,
    }
    return {
        "schema_version": CHANGE_IMPACT_SCHEMA_VERSION,
        "preview_digest": _sha256(core),
        **core,
        "decision_card": {
            "ready": ready,
            "patterns": patterns[:16],
            "bestExample": best_examples,
            "impact": core["impact"],
            "selectedTests": selected_tests,
            "whatWouldHelp": what_would_help,
            "confidence": confidence,
            "stale": bool(conventions_stale or graph_stale),
            "graph_stale": graph_stale,
            "low_confidence": low_confidence,
            "conflicts": conflicts,
        },
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_mem0": False, "model_started": False, "auto_apply": False, "authority": "proposal-only"},
        "gates": {"graph_digest_bound": True, "coverage_complete": coverage, "stale_rejected": not (conventions_stale or graph_stale), "conflicts_rejected": not conflicts, "low_confidence_rejected": not low_confidence, "human_review_required": True},
    }


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
    completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", cwd=root, env=environment)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], check=True, capture_output=True, text=True, encoding="utf-8", cwd=root, env=environment)
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
    command = ["git", "diff", "--unified=0", revision, "--"]
    selected = sorted({_path(path) for path in (paths or []) if str(path or "").strip()})[:MAX_CHANGED_PATHS]
    command.extend(selected)
    completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", cwd=root, env=environment)
    current_path = ""
    hunks: list[dict[str, Any]] = []
    hunk_pattern = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")
    for line in completed.stdout.splitlines():
        if line.startswith("+++ b/"):
            try:
                current_path = _path(line[6:])
            except ChangeImpactError:
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
    completed = subprocess.run(["git", "log", f"--max-count={int(max_commits)}", "--format=%H", "--name-only", "--diff-filter=ACMRTUXB", "--"], check=True, capture_output=True, text=True, encoding="utf-8", cwd=root, env=environment)
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


__all__ = ["CHANGE_IMPACT_SCHEMA_VERSION", "IMPACT_BINDING_SCHEMA_VERSION", "DIFF_HUNK_SUMMARY_SCHEMA_VERSION", "GIT_HISTORY_CORRELATION_SCHEMA_VERSION", "ChangeImpactError", "build_change_impact_preview", "build_impact_binding_receipt", "build_cross_repo_history_preview", "build_git_history_correlation_receipt", "build_git_symbol_impact_evidence", "collect_git_change_paths", "collect_git_diff_hunks", "collect_git_history_stats", "correlate_diff_hunks_to_symbols", "correlate_git_history_to_symbols", "summarize_diff_hunks"]
