#!/usr/bin/env python
"""Validate a bounded Git-to-symbol impact receipt without side effects.

WI-114 binds existing proposal-only WI-83 impact evidence to an explicit graph
snapshot digest and bounded change/hunk/symbol coverage.  This validator only
reads a captured JSON object; it never runs Git, starts a service, applies a
patch, writes a store or returns source text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "bhm.p28.wi114.git-impact-receipt.v1"
IMPACT_SCHEMA_VERSION = "bhm.change-impact.git-symbols.v1"
MAX_CHANGED_PATHS = 64
MAX_DIFF_HUNKS = 128
MAX_SYMBOLS = 128


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_execution(payload: Mapping[str, Any]) -> bool:
    execution = payload.get("execution") if isinstance(payload.get("execution"), Mapping) else {}
    return all(execution.get(key) is not True for key in ("writes_sqlite_state", "writes_qdrant", "writes_worktree", "writes_mem0", "auto_apply"))


def build_git_impact_receipt(
    evidence: Mapping[str, Any],
    *,
    current_graph_digest: str | None = None,
    expected_graph_digest: str | None = None,
) -> dict[str, Any]:
    """Return explicit pass/gap/fail state for captured impact metadata."""

    failures: list[str] = []
    gaps: list[str] = []
    provenance = evidence.get("provenance") if isinstance(evidence.get("provenance"), Mapping) else {}
    changed_paths = evidence.get("changed_paths") if isinstance(evidence.get("changed_paths"), list) else []
    diff_hunks = evidence.get("diff_hunks") if isinstance(evidence.get("diff_hunks"), list) else []
    hunk_symbols = evidence.get("hunk_symbols") if isinstance(evidence.get("hunk_symbols"), list) else []
    history = evidence.get("git_history") if isinstance(evidence.get("git_history"), Mapping) else {}
    history_symbols = evidence.get("history_symbols") if isinstance(evidence.get("history_symbols"), list) else []
    impact_graph_digest = str(evidence.get("graph_digest") or "").strip()
    expected_digest = str(expected_graph_digest or current_graph_digest or "").strip()

    if str(evidence.get("schema_version") or "") != IMPACT_SCHEMA_VERSION:
        failures.append("impact_schema_mismatch")
    if not changed_paths:
        failures.append("changed_paths_missing")
    if len(changed_paths) > MAX_CHANGED_PATHS:
        failures.append("changed_paths_cap_exceeded")
    if len(diff_hunks) > MAX_DIFF_HUNKS:
        failures.append("diff_hunks_cap_exceeded")
    if len(hunk_symbols) > MAX_SYMBOLS or len(history_symbols) > MAX_SYMBOLS:
        failures.append("symbol_cap_exceeded")
    path_set = {str(path).replace("\\", "/") for path in changed_paths}
    hunk_paths = {str(item.get("path") or "").replace("\\", "/") for item in diff_hunks if isinstance(item, Mapping)}
    if hunk_paths and not hunk_paths.issubset(path_set):
        failures.append("hunk_path_outside_changed_paths")
    if not hunk_symbols:
        failures.append("hunk_symbol_correlation_missing")
    commits = history.get("commits_considered")
    try:
        history_present = int(commits or 0) >= 1
    except (TypeError, ValueError):
        history_present = False
    if not history_present:
        failures.append("git_history_missing")
    if not _safe_execution(evidence):
        failures.append("proposal_only_execution_boundary_failed")
    if provenance.get("raw_source_returned") is not False or provenance.get("git_metadata_only") is not True or provenance.get("graph_metadata_only") is not True:
        failures.append("metadata_only_provenance_failed")
    if expected_digest:
        graph_digest_aligned = bool(impact_graph_digest and impact_graph_digest == expected_digest)
        if not graph_digest_aligned:
            failures.append("graph_digest_mismatch")
    else:
        graph_digest_aligned = False
        gaps.append("graph_digest_binding_missing")

    checks = {
        "schema_compatible": str(evidence.get("schema_version") or "") == IMPACT_SCHEMA_VERSION,
        "changed_paths_bounded": bool(changed_paths) and len(changed_paths) <= MAX_CHANGED_PATHS,
        "diff_hunks_bounded": len(diff_hunks) <= MAX_DIFF_HUNKS,
        "symbols_bounded": len(hunk_symbols) <= MAX_SYMBOLS and len(history_symbols) <= MAX_SYMBOLS,
        "hunk_paths_covered": not hunk_paths or hunk_paths.issubset(path_set),
        "hunk_symbol_correlation": bool(hunk_symbols),
        "history_present": history_present,
        "graph_digest_aligned": graph_digest_aligned,
        "proposal_only": _safe_execution(evidence),
        "metadata_only": provenance.get("raw_source_returned") is False and provenance.get("git_metadata_only") is True and provenance.get("graph_metadata_only") is True,
    }
    status = "fail" if failures else ("gap" if gaps else "pass")
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "ok": status == "pass",
        "checks": checks,
        "gaps": sorted(set(gaps)),
        "failures": sorted(set(failures)),
        "graph_binding": {"impact_graph_digest": impact_graph_digest, "expected_graph_digest": expected_digest, "aligned": graph_digest_aligned},
        "coverage": {"changed_paths": len(changed_paths), "diff_hunks": len(diff_hunks), "hunk_symbols": len(hunk_symbols), "history_symbols": len(history_symbols), "caps": {"changed_paths": MAX_CHANGED_PATHS, "diff_hunks": MAX_DIFF_HUNKS, "symbols": MAX_SYMBOLS}},
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_mem0": False, "writes_worktree": False, "auto_apply": False, "raw_source_returned": False, "network_writes": False},
    }
    receipt["evidence_digest"] = _digest({key: value for key, value in receipt.items() if key != "evidence_digest"})
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--graph-digest", default="")
    args = parser.parse_args()
    evidence = json.loads(args.input.read_text(encoding="utf-8-sig"))
    if not isinstance(evidence, Mapping):
        raise SystemExit("input must be a JSON object")
    receipt = build_git_impact_receipt(evidence, current_graph_digest=args.graph_digest or None)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
