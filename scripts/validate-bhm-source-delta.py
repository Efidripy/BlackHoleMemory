#!/usr/bin/env python3
"""Validate the explicit disposition of remaining CBM/research source deltas."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from blackholememory.filesystem_boundaries import replace_bytes_safely


DISPOSITIONS = {
    "CXM": ("no-adoption-required", "hash/dedupe and timeline/detail retrieval are already BHM-native; no incremental delta is demonstrated", ["src/blackholememory/repository_index.py", "src/blackholememory/app.py", "tests/unit/test_repository_index.py"]),
    "AXM": ("no-adoption-required", "tiered disclosure, retention and durable task state are already represented by BHM primitives; no second workflow authority is allowed", ["src/blackholememory/retention.py", "src/blackholememory/task_graph.py", "tests/unit/test_task_graph.py"]),
    "PMB": ("no-adoption-required", "usefulness feedback and no-LLM read-path behavior are covered; the web-only claim is not a pinned implementation source", ["src/blackholememory/adaptive_profile.py", "src/blackholememory/feedback_tuning.py", "tests/unit/test_adaptive_profile.py"]),
    "LCM": ("reference-only", "event-log/replay concepts remain a benchmark hypothesis; no pinned source and no duplicate Rust/events authority may enter BHM", [".docs/ops/bhm-p20.7-wi06-memory-graph-2026-07-16.md", "tests/integration/test_memory_graph.py"]),
    "JCG": ("reference-only", "query UX and pagination are reference criteria only; repository license remains unverified and no runtime namespace is added", ["src/blackholememory/code_graph_query.py", "tests/integration/test_code_graph_query.py"]),
    "MNM": ("reference-only", "public UX claims are not an auditable source implementation and remain optional review criteria", ["src/blackholememory/session_capture.py", "tests/integration/test_session_capture.py"]),
    "ARC": ("unavailable", "acquisition is retained as HTTP-429 evidence; no auditable source was obtained", [".src/archivist/FETCH-ERROR.txt", ".docs/ops/bhm-p20.0-wi00-source-passport-2026-07-16.md"]),
    "OVG": ("reference-only", "closed hosted product; acceptance criteria only, with no external runtime dependency", ["src/blackholememory/human_ui_bridge.py", "tests/integration/test_wi12_human_ui.py"]),
    "CAV": ("reference-only", "commercial hosted documentation; public UX reference only, no code or service dependency", ["src/blackholememory/unified_context.py", "tests/integration/test_unified_context.py"]),
    "RAV": ("reference-only", "hosted early-access claims are benchmark hypotheses only; no source or runtime adapter is admitted", ["src/blackholememory/repository_index.py", "src/blackholememory/change_impact.py"]),
    "OBS": ("reference-only", "Obsidian is an optional Markdown review surface and never a BHM authority or runtime dependency", ["src/blackholememory/human_ui_bridge.py", "tests/integration/test_wi12_human_ui.py"]),
    "OGV": ("reference-only", "Obsidian graph-view documentation supplies UX acceptance criteria only; no plugin dependency is introduced", ["src/blackholememory/human_ui_bridge.py", ".docs/ops/bhm-p20.13-wi12-human-ui-2026-07-16.md"]),
    "OCG": ("reference-only", "community plugin license is unverified; Obsidian remains optional and non-authoritative", ["src/blackholememory/code_graph.py", ".docs/ops/bhm-p20.13-wi12-human-ui-2026-07-16.md"]),
    "KGF": ("reference-only", "community graph-curation UX is optional review guidance, not a runtime authority", ["src/blackholememory/human_ui_bridge.py", "tests/unit/test_human_ui_bridge.py"]),
    "EVS": ("reference-only", "local-sync description is retained as an export/import acceptance criterion; no plugin dependency is introduced", ["src/blackholememory/migration_compatibility.py", "tests/unit/test_migration_compatibility.py"]),
}


def _write_report(path: Path, report: dict) -> None:
    replace_bytes_safely(path, (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    registry = json.loads(Path("config/source-registry.json").read_text(encoding="utf-8"))
    sources = {item["id"]: item for item in registry["sources"]}
    entries = []
    failures = []
    for source_id, (outcome, reason, evidence) in DISPOSITIONS.items():
        source = sources.get(source_id)
        manifest_paths = sorted(Path(".src").glob("*/SOURCE-MANIFEST.json"))
        manifest_path = next((path for path in manifest_paths if json.loads(path.read_text(encoding="utf-8")).get("source_id") == source_id), None)
        existing = [path for path in evidence if Path(path).exists()]
        if source is None or manifest_path is None or not existing:
            failures.append(source_id)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path else {}
        entries.append({
            "source_id": source_id,
            "name": source.get("name") if source else None,
            "outcome": outcome,
            "value": source.get("purpose", []) if source else [],
            "reason": reason,
            "risk": "foreign code/runtime authority, provenance drift or duplicate memory authority" if outcome != "unavailable" else "evidence cannot be rechecked until a pinned source is available",
            "license": manifest.get("license"),
            "permission_status": manifest.get("permission_status"),
            "code_copy_allowed": bool(manifest.get("code_copy_allowed")),
            "evidence": existing,
            "evidence_hashes": {path: sha256(Path(path)) for path in existing},
            "rollback": "remove reference-only ledger entry; no runtime/data rollback required",
        })
    report = {
        "schema_version": "bhm.p21.17.wi35.source-delta.v1",
        "generated_at": "2026-07-21",
        "plan_id": "BHM-V5-POST-ACCEPTANCE-20260717",
        "scope": sorted(DISPOSITIONS),
        "entries": entries,
        "unknown_or_undispositioned": failures,
        "code_copy_allowed_count": sum(1 for item in entries if item["code_copy_allowed"]),
        "adopted_delta_count": sum(1 for item in entries if item["outcome"] == "adopt"),
        "runtime_dependency_count": 0,
        "writes_live_state": False,
        "ok": not failures and all(item["outcome"] in {"adopt", "already-equivalent", "no-adoption-required", "reference-only", "rejected", "unavailable"} for item in entries),
    }
    _write_report(args.report, report)
    print(json.dumps({"ok": report["ok"], "entries": len(entries), "failures": failures, "adopted": report["adopted_delta_count"]}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
