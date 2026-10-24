"""Read-only command contract for the SQLite-authoritative memory doctor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from typing import Sequence

from .filesystem_boundaries import replace_bytes_safely
from .mem0_adapter import get_qdrant_client
from .mem0_adapter import local_collection_name
from .memory_doctor import MemoryDoctorSnapshotError
from .memory_doctor import run_authoritative_projection_memory_doctor
from .memory_doctor import run_authoritative_sqlite_memory_doctor
from .project_registry import canonical_project_id


SCHEMA_VERSION = "bhm.memory-doctor.cli.v1"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATABASE = _REPO_ROOT / ".runtime" / "live-memory" / "memories.sqlite3"


def build_read_only_memory_doctor_report(
    database: Path | str,
    *,
    project: str | None = None,
    limit: int = 10_000,
    projection_client: Any | None = None,
    projection_collection: str | None = None,
) -> dict[str, Any]:
    """Run the authority doctor without disclosing the database path or writing.

    The underlying snapshot opens SQLite with ``mode=ro`` and ``query_only``.
    When an injected Qdrant client is supplied, it performs one bounded
    ``scroll`` of the declared local projection with vectors disabled.  It has
    no repair, backup, migration or apply mode; broader maintenance remains
    separately gated.
    """

    project_raw = str(project or "").strip()
    project_value = canonical_project_id(project_raw) if project_raw else None
    projection_enabled = projection_client is not None
    if projection_enabled:
        if project_value is None:
            raise MemoryDoctorSnapshotError("--projection requires an explicit project scope")
        collection = str(projection_collection or local_collection_name(project_value)).strip()
        report = run_authoritative_projection_memory_doctor(
            database,
            projection_client,
            [collection],
            project=project_value,
            limit=limit,
            expected_collections_by_project={project_value: [collection]} if project_value else None,
        )
    else:
        report = run_authoritative_sqlite_memory_doctor(
            database,
            project=project_value,
            limit=limit,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "doctor": report,
        "scope": {
            "authority": "sqlite-authoritative",
            "projection_checked": projection_enabled,
            "repair_available": False,
            "database_path_disclosed": False,
        },
        "execution": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "backup_created": False,
            "repair_apply": False,
            "auto_apply": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=_DEFAULT_DATABASE)
    parser.add_argument("--project", default="")
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument(
        "--projection",
        action="store_true",
        help="also inspect the declared local Qdrant projection with a bounded read-only scroll",
    )
    parser.add_argument("--report", type=Path, help="optional local JSON output path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_read_only_memory_doctor_report(
            args.database.expanduser(),
            project=args.project,
            limit=args.limit,
            projection_client=get_qdrant_client() if args.projection else None,
        )
    except (MemoryDoctorSnapshotError, OSError, ValueError) as exc:
        _parser().error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report is not None:
        replace_bytes_safely(args.report.expanduser().resolve(), (rendered + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
