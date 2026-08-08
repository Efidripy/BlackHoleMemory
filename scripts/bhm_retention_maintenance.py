#!/usr/bin/env python
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.hook_queue import HookJobQueue
from blackholememory.observation_store import ObservationStore
from blackholememory.retention import RetentionPolicyError
from blackholememory.retention import apply_retention_plan
from blackholememory.retention import build_retention_plan
from blackholememory.retention import create_retention_backup
from blackholememory.retention import load_retention_policy
from blackholememory.retention import parse_timestamp
from blackholememory.retention import restore_retention_backup
from blackholememory.retention import summarize_retention_plan
from blackholememory.retention import utc_iso
from blackholememory.filesystem_boundaries import replace_bytes_safely


DEFAULT_RUNTIME_DIR = REPO_ROOT / ".runtime" / "live-memory"
DEFAULT_POLICY_PATH = REPO_ROOT / "config" / "retention-policy.json"
REPORT_ROOT = REPO_ROOT / ".runtime" / "reports"


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    target = path.expanduser().resolve(strict=False)
    root = REPORT_ROOT.expanduser().resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"report path must stay under approved report root: {root}") from exc
    cursor = target.parent
    while cursor != root:
        if cursor.is_symlink():
            raise OSError(f"report parent must not be a symlink: {cursor}")
        cursor = cursor.parent
    replace_bytes_safely(
        target,
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _parse_as_of(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = parse_timestamp(value)
    if parsed is None:
        raise RetentionPolicyError(f"invalid --as-of timestamp: {value}")
    return parsed


def _parse_rules(value: str) -> set[str] | None:
    names = {item.strip() for item in value.split(",") if item.strip()}
    return names or None


def _validate_selected_rules(selected: set[str] | None, policy) -> None:
    if selected is None:
        return
    available = {
        *(rule.name for rule in policy.observation_rules),
        *(rule.name for rule in policy.hook_job_rules),
        "explicit-purge",
    }
    unknown = sorted(selected - available)
    if unknown:
        raise RetentionPolicyError(f"unknown retention rules: {', '.join(unknown)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first TTL, sampling and retention maintenance for BHM observations and hook jobs."
    )
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--project")
    parser.add_argument("--rules", default="", help="comma-separated exact policy rule names")
    parser.add_argument("--as-of", default="", help="fixed ISO-8601 timestamp; required for apply")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-digest", default="")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--max-expire", type=int, default=1000)
    parser.add_argument("--restore-manifest", type=Path)
    parser.add_argument("--restore-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.restore_manifest:
            if args.apply or args.backup_dir or args.confirm_plan_digest:
                raise RetentionPolicyError("restore mode cannot be combined with apply options")
            if args.restore_dir is None:
                raise RetentionPolicyError("--restore-dir is required with --restore-manifest")
            report = {
                "success": True,
                "mode": "restore-staging",
                "restore": restore_retention_backup(args.restore_manifest, args.restore_dir),
            }
            _write_report(args.report, report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        if args.restore_dir is not None:
            raise RetentionPolicyError("--restore-dir requires --restore-manifest")
        if args.max_expire < 0:
            raise RetentionPolicyError("--max-expire must be non-negative")
        if args.apply and not args.as_of:
            raise RetentionPolicyError("--apply requires the exact --as-of value from the reviewed dry-run")
        if args.apply and not args.confirm_plan_digest:
            raise RetentionPolicyError("--apply requires --confirm-plan-digest from the reviewed dry-run")
        if args.apply and args.backup_dir is None:
            raise RetentionPolicyError("--apply requires an explicit --backup-dir")

        runtime_dir = args.runtime_dir.resolve()
        policy = load_retention_policy(args.policy)
        selected_rules = _parse_rules(args.rules)
        _validate_selected_rules(selected_rules, policy)
        as_of = _parse_as_of(args.as_of)
        observation_store = ObservationStore(runtime_dir / "observations.sqlite3")
        hook_queue = HookJobQueue(runtime_dir / "hook-jobs.sqlite3")
        plan = build_retention_plan(
            observation_store.retention_candidates(project=args.project),
            hook_queue.retention_candidates(project=args.project),
            policy,
            as_of=as_of,
            selected_rules=selected_rules,
        )
        summary = summarize_retention_plan(plan)
        before = {
            "observations": observation_store.status(integrity_check=True),
            "hookQueue": hook_queue.status(integrity_check=True),
        }
        report: dict[str, Any] = {
            "success": True,
            "mode": "apply" if args.apply else "dry-run",
            "runtimeDir": str(runtime_dir),
            "project": args.project or "",
            "asOf": utc_iso(as_of),
            "plan": summary,
            "before": before,
        }

        if args.apply:
            if args.confirm_plan_digest != str(plan.get("planDigest") or ""):
                raise RetentionPolicyError(
                    "retention plan digest changed; rerun dry-run and review the new digest before apply"
                )
            backup_manifest = create_retention_backup(
                observation_store,
                hook_queue,
                args.backup_dir,
                plan_summary=summary,
            )
            report["backupManifest"] = str(backup_manifest)
            report["apply"] = apply_retention_plan(
                plan,
                observation_store,
                hook_queue,
                max_expire=args.max_expire,
            )
            report["after"] = {
                "observations": observation_store.status(integrity_check=True),
                "hookQueue": hook_queue.status(integrity_check=True),
            }

        _write_report(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        error = {
            "success": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }
        try:
            _write_report(args.report, error)
        except Exception:
            pass
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
