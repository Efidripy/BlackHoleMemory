"""Read-only preflight for the JSON-sidecar versus SQLite authority boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "bhm.sidecar-authority-preflight.v1"
SIDECAR_NAMES = (
    "slots.json",
    "lessons.json",
    "memory-links.json",
    "checkpoints.json",
    "project-maps.json",
    "adrs.json",
    "handoffs.json",
    "session-records.json",
    "tasks.json",
    "task-contexts.json",
    "risk-registers.json",
    "validation-snapshots.json",
    "entity-catalogs.json",
    "policy-profile.json",
)

# These are planning dispositions only.  They do not authorize migration and
# deliberately keep unknown compatibility artifacts out of SQLite until a
# deterministic field-level mapping and rollback receipt exist.
SIDECAR_DISPOSITIONS: dict[str, dict[str, Any]] = {
    "slots.json": {"disposition": "retain_compatibility_read_model", "candidate_sqlite_surfaces": []},
    "lessons.json": {"disposition": "retain_compatibility_read_model", "candidate_sqlite_surfaces": []},
    "memory-links.json": {"disposition": "candidate_for_staged_mapping", "candidate_sqlite_surfaces": ["memory_links"]},
    "checkpoints.json": {"disposition": "candidate_for_staged_mapping", "candidate_sqlite_surfaces": ["memory_artifacts"]},
    "session-records.json": {"disposition": "candidate_for_staged_mapping", "candidate_sqlite_surfaces": ["memory_artifacts"]},
    "tasks.json": {"disposition": "candidate_for_staged_mapping", "candidate_sqlite_surfaces": ["task_graph_current", "task_graph_snapshots"]},
    "project-maps.json": {"disposition": "unmapped_compatibility_artifact", "candidate_sqlite_surfaces": []},
    "adrs.json": {"disposition": "unmapped_compatibility_artifact", "candidate_sqlite_surfaces": []},
    "handoffs.json": {"disposition": "unmapped_compatibility_artifact", "candidate_sqlite_surfaces": []},
    "task-contexts.json": {"disposition": "unmapped_compatibility_artifact", "candidate_sqlite_surfaces": []},
    "risk-registers.json": {"disposition": "unmapped_compatibility_artifact", "candidate_sqlite_surfaces": []},
    "validation-snapshots.json": {"disposition": "unmapped_compatibility_artifact", "candidate_sqlite_surfaces": []},
    "entity-catalogs.json": {"disposition": "unmapped_compatibility_artifact", "candidate_sqlite_surfaces": []},
    "policy-profile.json": {"disposition": "retain_runtime_policy_artifact", "candidate_sqlite_surfaces": []},
}


def _record_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return 1
    return 0


def _duplicate_keys(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    keys: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        for field in ("id", "source_id", "upsert_key", "label", "queue_id"):
            candidate = item.get(field)
            if candidate not in (None, ""):
                keys.append(f"{field}:{candidate}")
                break
    seen: set[str] = set()
    duplicates: set[str] = set()
    for key in keys:
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return sorted(duplicates)


def _inspect_sidecar(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": path.name,
        "path": str(path),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else 0,
        "parse_status": "missing",
        "record_count": 0,
        "duplicate_keys": [],
        "sha256": None,
        **SIDECAR_DISPOSITIONS.get(
            path.name,
            {"disposition": "unmapped_compatibility_artifact", "candidate_sqlite_surfaces": []},
        ),
    }
    if not path.exists():
        return result
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result["parse_status"] = "error"
        result["error"] = type(exc).__name__
        return result
    result["parse_status"] = "ok"
    result["record_count"] = _record_count(value)
    result["duplicate_keys"] = _duplicate_keys(value)
    result["top_level_type"] = type(value).__name__
    return result


def _inspect_sqlite(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "open_mode": "read-only",
        "schema_status": "missing",
        "tables": [],
        "memory_count": None,
        "outbox_counts": {},
    }
    if not path.exists():
        return result
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            result["tables"] = tables
            result["schema_status"] = "ok" if "memories" in tables and "memory_outbox" in tables else "incomplete"
            if "memories" in tables:
                result["memory_count"] = int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            if "memory_outbox" in tables:
                result["outbox_counts"] = {
                    str(status): int(count)
                    for status, count in connection.execute(
                        "SELECT status, COUNT(*) FROM memory_outbox GROUP BY status"
                    )
                }
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        result["schema_status"] = "error"
        result["error"] = type(exc).__name__
    return result


def build_report(repo_root: Path, runtime_dir: Path | None = None) -> dict[str, Any]:
    root = repo_root.resolve()
    live_memory = (runtime_dir or root / ".runtime" / "live-memory").resolve()
    sidecars = [_inspect_sidecar(live_memory / name) for name in SIDECAR_NAMES]
    sqlite = _inspect_sqlite(live_memory / "memories.sqlite3")
    parse_errors = [item["name"] for item in sidecars if item["parse_status"] == "error"]
    duplicate_files = [item["name"] for item in sidecars if item["duplicate_keys"]]
    present_sidecars = [item for item in sidecars if item["exists"]]
    sqlite_schema_ready = sqlite["schema_status"] == "ok"
    reconciliation_ready = not present_sidecars and not parse_errors and sqlite_schema_ready
    return {
        "schema_version": SCHEMA_VERSION,
        # ``ok`` means that this read-only preflight completed and the
        # authoritative schema can be inspected. It deliberately does not
        # claim that JSON sidecar reconciliation is complete.
        "ok": not parse_errors and sqlite_schema_ready,
        "read_only": True,
        "authority_state": (
            "sqlite_authoritative"
            if reconciliation_ready
            else "sqlite_authoritative_with_sidecar_residual"
        ),
        "reconciliation_ready": reconciliation_ready,
        "migration_required": bool(present_sidecars),
        "split_brain_risk": bool(present_sidecars),
        "sidecar_count": len(present_sidecars),
        "sidecar_record_count": sum(int(item["record_count"]) for item in present_sidecars),
        "disposition_counts": {
            disposition: sum(1 for item in present_sidecars if item["disposition"] == disposition)
            for disposition in sorted({str(item["disposition"]) for item in present_sidecars})
        },
        "staged_mapping_required": any(
            item["disposition"] == "candidate_for_staged_mapping" for item in present_sidecars
        ),
        "parse_errors": parse_errors,
        "duplicate_key_files": duplicate_files,
        "sidecars": sidecars,
        "sqlite": sqlite,
        "next_action": (
            "Create hash-verified backup and staged reconciliation plan before any migration."
            if present_sidecars
            else "No JSON sidecars are present; retain read-only gate."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--as-json", action="store_true")
    args = parser.parse_args()
    report = build_report(args.repo_root, args.runtime_dir)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"sidecars={report['sidecar_count']} records={report['sidecar_record_count']}")
        print(f"sqlite={report['sqlite']['schema_status']} read_only={report['read_only']}")
        print(
            f"authority_state={report['authority_state']} "
            f"reconciliation_ready={report['reconciliation_ready']}"
        )
        print(f"migration_required={report['migration_required']} split_brain_risk={report['split_brain_risk']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
