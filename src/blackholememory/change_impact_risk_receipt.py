"""Bounded metadata-only risk receipt for change-impact previews."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


CHANGE_IMPACT_RISK_RECEIPT_SCHEMA_VERSION = "bhm.change-impact.risk-receipt.v1"
MAX_RISK_ITEMS = 512


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _bounded_count(value: Any) -> int:
    try:
        return max(0, min(int(value or 0), MAX_RISK_ITEMS))
    except (TypeError, ValueError, OverflowError):
        return 0


def build_change_impact_risk_receipt(
    impact_preview: Mapping[str, Any],
    *,
    changed_paths: Sequence[str],
    diff_hunks: Sequence[Mapping[str, Any]],
    hunk_symbols: Sequence[Mapping[str, Any]],
    git_history: Mapping[str, Any] | None = None,
    impact_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify bounded impact metadata without reading source or applying changes."""

    preview = impact_preview if isinstance(impact_preview, Mapping) else {}
    history = git_history if isinstance(git_history, Mapping) else {}
    binding = impact_binding if isinstance(impact_binding, Mapping) else {}
    selected_tests = preview.get("selectedTests") if isinstance(preview.get("selectedTests"), list) else []
    conflicts = preview.get("conflicts") if isinstance(preview.get("conflicts"), list) else []
    counts = {
        "changed_paths": _bounded_count(len(list(changed_paths)[:MAX_RISK_ITEMS])),
        "diff_hunks": _bounded_count(len(list(diff_hunks)[:MAX_RISK_ITEMS])),
        "hunk_symbols": _bounded_count(len(list(hunk_symbols)[:MAX_RISK_ITEMS])),
        "selected_tests": _bounded_count(len(selected_tests)),
        "git_hotspots": _bounded_count(len(history.get("hotspots") or [])),
        "git_commits": _bounded_count(history.get("commits_considered")),
        "conflicts": _bounded_count(len(conflicts)),
    }
    graph_bound = bool(str(binding.get("graph_snapshot_id") or preview.get("graph_snapshot_id") or "").strip() and str(binding.get("graph_digest") or preview.get("graph_digest") or "").strip())
    binding_coverage = binding.get("coverage") if isinstance(binding.get("coverage"), Mapping) else {}
    binding_checks = binding.get("checks") if isinstance(binding.get("checks"), Mapping) else {}
    coverage_complete = bool(
        binding_coverage.get("complete")
        or (binding_checks and all(binding_checks.get(key) is True for key in ("graph_snapshot_bound", "changed_paths_bounded", "hunk_paths_covered", "hunk_symbol_coverage", "history_present")))
        or preview.get("gates", {}).get("coverage_complete")
    )
    stale = bool(preview.get("stale") or preview.get("graph_stale"))
    low_confidence = bool(preview.get("low_confidence"))
    ready = bool(preview.get("ready"))
    history_available = bool(history.get("available", bool(history.get("commits_considered") or history.get("hotspots"))))
    high_signal = counts["conflicts"] > 0 or stale or low_confidence or counts["diff_hunks"] > 32 or counts["hunk_symbols"] > 64
    medium_signal = counts["changed_paths"] > 8 or counts["diff_hunks"] > 8 or counts["hunk_symbols"] > 16 or counts["selected_tests"] > 8 or not graph_bound or not coverage_complete
    risk_bucket = "high" if high_signal else "medium" if medium_signal else "low"
    gaps: list[str] = []
    if not graph_bound:
        gaps.append("graph_binding_missing")
    if not coverage_complete:
        gaps.append("impact_coverage_incomplete")
    if not history_available:
        gaps.append("git_history_missing")
    if not changed_paths:
        gaps.append("changed_paths_missing")
    core = {
        "schema_version": CHANGE_IMPACT_RISK_RECEIPT_SCHEMA_VERSION,
        "status": "observed" if ready and not gaps and not high_signal else "review_required",
        "risk_bucket": risk_bucket,
        "disposition": "review_required",
        "coverage": {**counts, "graph_bound": graph_bound, "complete": coverage_complete, "history_available": history_available},
        "signals": {"ready": ready, "stale": stale, "low_confidence": low_confidence, "conflicts": bool(conflicts)},
        "gaps": sorted(set(gaps)),
        "evidence_digest": _digest({"preview_digest": preview.get("preview_digest"), "binding_digest": binding.get("evidence_digest"), "counts": counts}),
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "network": False, "model_started": False, "raw_source_returned": False, "raw_diff_returned": False, "autonomous_apply": False, "edge_promotion": False},
    }
    core["receipt_digest"] = _digest({key: value for key, value in core.items() if key != "receipt_digest"})
    return core


__all__ = ["CHANGE_IMPACT_RISK_RECEIPT_SCHEMA_VERSION", "build_change_impact_risk_receipt"]
