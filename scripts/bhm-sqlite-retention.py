#!/usr/bin/env python3
"""Plan or apply bounded retention to the authoritative BHM SQLite store."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# ruff: noqa: E402
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.sqlite_retention import SQLiteRetentionError
from blackholememory.sqlite_retention import SQLiteRetentionPolicy
from blackholememory.sqlite_retention import apply_sqlite_retention
from blackholememory.sqlite_retention import compact_sqlite_database
from blackholememory.sqlite_retention import create_verified_sqlite_backup
from blackholememory.sqlite_retention import plan_sqlite_retention
from blackholememory.sqlite_retention import utc_now_iso
from blackholememory.sqlite_retention import verify_sqlite_database


def _default_database() -> Path:
    return REPO_ROOT / ".runtime" / "live-memory" / "memories.sqlite3"


def _listener_open(host: str = "127.0.0.1", port: int = 8000) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=_default_database())
    parser.add_argument(
        "--as-of", default="", help="fixed UTC timestamp; required for apply"
    )
    parser.add_argument("--keep-graph-history", type=int, default=2)
    parser.add_argument("--keep-index-history", type=int, default=2)
    parser.add_argument("--keep-completed-outbox", type=int, default=1_000)
    parser.add_argument("--keep-latest-outbox-per-aggregate", type=int, default=1)
    parser.add_argument("--graph-min-age-days", type=int, default=7)
    parser.add_argument("--index-min-age-days", type=int, default=7)
    parser.add_argument("--outbox-min-age-days", type=int, default=30)
    parser.add_argument("--max-graph-snapshots", type=int, default=8)
    parser.add_argument("--max-index-snapshots", type=int, default=8)
    parser.add_argument("--max-outbox-events", type=int, default=1_000)
    parser.add_argument(
        "--apply", action="store_true", help="apply the reviewed retention plan"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="confirm all BHM SQLite writers are stopped",
    )
    parser.add_argument("--confirm-plan-digest", default="")
    parser.add_argument(
        "--backup", type=Path, help="required new rollback database for --apply"
    )
    parser.add_argument(
        "--reuse-backup",
        action="store_true",
        help="reuse and re-verify an existing rollback database after a pre-commit retry",
    )
    parser.add_argument(
        "--until-stable",
        action="store_true",
        help="continue bounded cycles under the same policy after the confirmed first batch",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="compact once after all retention cycles; never used by automatic cleanup",
    )
    parser.add_argument("--receipt", type=Path, help="optional JSON receipt path")
    return parser


def _policy(args: argparse.Namespace) -> SQLiteRetentionPolicy:
    return SQLiteRetentionPolicy(
        keep_graph_history_per_scope=args.keep_graph_history,
        keep_index_history_per_scope=args.keep_index_history,
        keep_completed_outbox=args.keep_completed_outbox,
        keep_latest_completed_outbox_per_aggregate=args.keep_latest_outbox_per_aggregate,
        graph_min_age_days=args.graph_min_age_days,
        index_min_age_days=args.index_min_age_days,
        completed_outbox_min_age_days=args.outbox_min_age_days,
        max_graph_snapshots_per_run=args.max_graph_snapshots,
        max_index_snapshots_per_run=args.max_index_snapshots,
        max_completed_outbox_per_run=args.max_outbox_events,
    )


def _candidate_count(plan: dict[str, object]) -> int:
    candidates = plan["candidates"]
    assert isinstance(candidates, dict)
    return sum(len(list(values)) for values in candidates.values())


def _compact_plan(plan: dict[str, object]) -> dict[str, object]:
    candidates = plan.get("candidates") or {}
    compact_candidates: dict[str, object] = {}
    if isinstance(candidates, dict):
        for name, values in candidates.items():
            ids = list(values)
            compact_candidates[name] = {"count": len(ids), "sample": ids[:10]}
    return {key: value for key, value in plan.items() if key != "candidates"} | {
        "candidates": compact_candidates
    }


def _ensure_free_space(database: Path, backup: Path, *, vacuum: bool) -> dict[str, int]:
    database_bytes = int(database.stat().st_size)
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup_free = int(shutil.disk_usage(backup.parent).free)
    required_backup = max(256 * 1024 * 1024, int(database_bytes * 1.10))
    if backup_free < required_backup:
        raise SQLiteRetentionError(
            f"insufficient backup free space: required={required_backup}, available={backup_free}"
        )
    database_free = int(shutil.disk_usage(database.parent).free)
    required_database = int(database_bytes * 1.10) if vacuum else 0
    if database_free < required_database:
        raise SQLiteRetentionError(
            f"insufficient database-volume free space for VACUUM: "
            f"required={required_database}, available={database_free}"
        )
    return {
        "database_bytes": database_bytes,
        "backup_volume_free_bytes": backup_free,
        "database_volume_free_bytes": database_free,
        "required_backup_bytes": required_backup,
        "required_vacuum_bytes": required_database,
    }


def main() -> int:
    args = _parser().parse_args()
    if args.vacuum and not args.apply:
        raise SystemExit("--vacuum requires --apply")
    if args.until_stable and not args.apply:
        raise SystemExit("--until-stable requires --apply")
    if args.apply and not args.offline:
        raise SystemExit("--apply requires --offline")
    if args.apply and not args.as_of:
        raise SystemExit("--apply requires exact --as-of from dry-run")
    if args.apply and not args.confirm_plan_digest:
        raise SystemExit("--apply requires --confirm-plan-digest")
    if args.apply and args.backup is None:
        raise SystemExit("--apply requires a new --backup rollback path")
    if args.reuse_backup and not args.apply:
        raise SystemExit("--reuse-backup requires --apply")

    policy = _policy(args)
    database = args.database.expanduser().resolve()
    report: dict[str, object] = {
        "generated_at": utc_now_iso(),
        "mode": "apply" if args.apply else "dry-run",
        "database": str(database),
        "policy": policy.__dict__,
        "ok": False,
    }
    try:
        if not args.apply:
            plan = plan_sqlite_retention(database, policy, as_of=args.as_of or None)
            report["retention"] = _compact_plan(plan)
            report["ok"] = not bool(plan["blocked"])
        else:
            if database == _default_database().resolve() and _listener_open():
                raise SQLiteRetentionError(
                    "live retention requires the BHM API listener on 127.0.0.1:8000 to be stopped"
                )
            assert args.backup is not None
            backup = args.backup.expanduser().resolve()
            report["free_space"] = _ensure_free_space(
                database, backup, vacuum=args.vacuum
            )
            report["before"] = verify_sqlite_database(database)
            if args.reuse_backup:
                if not backup.exists():
                    raise SQLiteRetentionError(
                        f"rollback backup does not exist: {backup}"
                    )
                report["backup"] = verify_sqlite_database(backup)
                if not bool(report["backup"]["ok"]):
                    raise SQLiteRetentionError(
                        "existing rollback backup failed verification"
                    )
                report["backup_reused"] = True
            else:
                report["backup"] = create_verified_sqlite_backup(database, backup)

            cycles: list[dict[str, object]] = []
            report["cycles"] = cycles
            report["retention_committed"] = False
            while True:
                plan = plan_sqlite_retention(database, policy, as_of=args.as_of)
                if not cycles and plan["plan_digest"] != args.confirm_plan_digest:
                    raise SQLiteRetentionError(
                        "SQLite retention plan digest mismatch after backup; rebuild dry-run"
                    )
                if plan["blocked"]:
                    raise SQLiteRetentionError(f"retention blocked: {plan['blockers']}")
                if _candidate_count(plan) == 0:
                    break
                applied = apply_sqlite_retention(
                    database,
                    policy,
                    expected_plan_digest=str(plan["plan_digest"]),
                    as_of=args.as_of,
                )
                cycles.append(
                    {
                        "plan_digest": plan["plan_digest"],
                        "estimated_deletes": plan["estimated_deletes"],
                        "remaining_after_batch": plan["remaining_after_batch"],
                        "deleted": applied.get("deleted"),
                    }
                )
                report["retention_committed"] = True
                if not args.until_stable:
                    break

            if args.vacuum:
                try:
                    report["compaction"] = compact_sqlite_database(database)
                except Exception as exc:
                    report["compaction_failed"] = f"{type(exc).__name__}: {exc}"
                    raise
            report["after"] = verify_sqlite_database(database)
            after = report["after"]
            assert isinstance(after, dict)
            report["ok"] = bool(after["ok"])
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.receipt:
        replace_bytes_safely(args.receipt.expanduser(), rendered.encode("utf-8"))
    print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
