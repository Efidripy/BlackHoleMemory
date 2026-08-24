#!/usr/bin/env python
"""Validate a captured WI-138 Git-history correlation receipt.

This validator is read-only: it consumes JSON evidence, performs no Git or
network calls, and never writes BHM stores, graph edges or worktrees.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "bhm.change-impact.git-history-correlation.v1"
MAX_PATHS = 64
MAX_COMMITS = 64
MAX_HOTSPOTS = 32
MAX_COCHANGE = 64
MAX_SYMBOLS = 128


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def validate_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    if str(receipt.get("schema_version") or "") != SCHEMA_VERSION:
        failures.append("schema_mismatch")
    paths = receipt.get("changed_paths") if isinstance(receipt.get("changed_paths"), list) else []
    window = receipt.get("history_window") if isinstance(receipt.get("history_window"), Mapping) else {}
    counts = receipt.get("counts") if isinstance(receipt.get("counts"), Mapping) else {}
    provenance = receipt.get("provenance") if isinstance(receipt.get("provenance"), Mapping) else {}
    execution = receipt.get("execution") if isinstance(receipt.get("execution"), Mapping) else {}
    try:
        commits = int(window.get("commits_considered") or 0)
        max_commits = int(window.get("max_commits") or 0)
    except (TypeError, ValueError):
        commits = max_commits = -1
    if len(paths) > MAX_PATHS:
        failures.append("path_cap_exceeded")
    if commits < 0 or max_commits < 1 or max_commits > MAX_COMMITS or commits > max_commits:
        failures.append("history_window_invalid")
    for key, cap, error in (("hotspots", MAX_HOTSPOTS, "hotspot_cap_exceeded"), ("cochange_pairs", MAX_COCHANGE, "cochange_cap_exceeded"), ("correlated_symbols", MAX_SYMBOLS, "symbol_cap_exceeded")):
        try:
            if int(counts.get(key) or 0) > cap:
                failures.append(error)
        except (TypeError, ValueError):
            failures.append(f"{key}_invalid")
    if provenance.get("raw_source_returned") is not False or provenance.get("git_metadata_only") is not True or provenance.get("graph_metadata_only") is not True:
        failures.append("metadata_provenance_failed")
    if any(execution.get(key) is True for key in ("writes_worktree", "writes_sqlite_state", "writes_qdrant", "writes_mem0", "cross_edges_promoted", "auto_apply")):
        failures.append("execution_boundary_failed")
    status = str(receipt.get("status") or "")
    if status not in {"pass", "gap"}:
        failures.append("status_invalid")
    result = {
        "schema_version": "bhm.p28.wi138-git-history-correlation-validation.v1",
        "status": "fail" if failures else status,
        "ok": not failures,
        "failures": sorted(set(failures)),
        "checks": {
            "schema": "schema_mismatch" not in failures,
            "bounds": not any(item.endswith("_cap_exceeded") or item == "history_window_invalid" for item in failures),
            "metadata_only": "metadata_provenance_failed" not in failures,
            "proposal_only": "execution_boundary_failed" not in failures,
        },
    }
    result["evidence_digest"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise SystemExit("input must be a JSON object")
    result = validate_receipt(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
