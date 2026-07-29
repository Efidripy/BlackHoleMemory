#!/usr/bin/env python
"""Read-only classification of Qdrant REVIEW orphan projections."""

from __future__ import annotations

# The script adds the repository's src directory before importing project modules.
# ruff: noqa: E402

import argparse
import json
import socket
import sys
from collections import Counter
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.projection_reconciliation import QdrantSurfaceAdapter
from blackholememory.projection_reconciliation import build_projection_reconciliation_plan
from blackholememory.projection_reconciliation import classify_projection_review_entries
from blackholememory.projection_reconciliation import projection_review_classification_digest
from blackholememory.runtime_endpoints import endpoint_parts
from blackholememory.runtime_storage import inspect_memory_store_schema


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


DEFAULT_DATABASE = REPO_ROOT / "runtime" / "live-memory" / "memories.sqlite3"


class ProjectionClassificationError(RuntimeError):
    """Raised when the read-only classification cannot be built safely."""


DEFAULT_BHM_HOST, DEFAULT_BHM_PORT = endpoint_parts("bhm_api")


def _listener_open(host: str = DEFAULT_BHM_HOST, port: int = DEFAULT_BHM_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _as_of(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectionClassificationError(f"invalid --as-of timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--project", default=None)
    parser.add_argument("--as-of", default="")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--summary-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        database = args.database.expanduser().resolve()
        schema_ok, schema_reason = inspect_memory_store_schema(database)
        if not schema_ok:
            raise ProjectionClassificationError(
                f"SQLite target is not schema-valid: {database} ({schema_reason})"
            )
        as_of = _as_of(args.as_of)
        repository = SQLiteMemoryRepository(database)
        memories = repository.list_memories(
            project=args.project,
            include_archived=True,
            include_tombstoned=True,
            limit=10_000,
        )
        plan = build_projection_reconciliation_plan(
            repository,
            QdrantSurfaceAdapter(get_qdrant_client()),
            project=args.project,
            as_of=as_of,
        )
        classified = classify_projection_review_entries(
            plan,
            known_memory_ids={memory.id for memory in memories},
        )
        entries = [item.to_dict() for item in classified]
        counts = Counter(item["disposition"] for item in entries)
        surface_counts = Counter(item["surface"] for item in entries)
        matrix_counts = Counter(
            f"{item['surface']}|{item['disposition']}" for item in entries
        )
        report: dict[str, Any] = {
            "success": True,
            "mode": "dry-run",
            "database": str(database),
            "project": args.project,
            "asOf": as_of,
            "readOnlyRehearsal": True,
            "writes_live_state": False,
            "writerBoundary": {
                "apiListenerOpen": _listener_open(),
                "applySupported": False,
            },
            "sqliteMemoryCount": len(memories),
            "reconciliation": {
                "counts": plan.counts,
                "blockingIssues": len(plan.blocking_issues),
                "planDigest": plan.digest,
            },
            "classification": {
                "counts": dict(sorted(counts.items())),
                "surfaceCounts": dict(sorted(surface_counts.items())),
                "matrixCounts": dict(sorted(matrix_counts.items())),
                "classificationDigest": projection_review_classification_digest(classified),
                "entries": entries,
            },
        }
        _write_report(args.report, report)
        if args.summary_only:
            output = {
                key: value
                for key, value in report.items()
                if key != "classification"
            }
            output["classification"] = {
                key: value
                for key, value in report["classification"].items()
                if key != "entries"
            }
        else:
            output = report
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        error = {
            "success": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
