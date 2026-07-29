#!/usr/bin/env python3
"""Explicit CLI for WI-01 repository state, index and bounded polling."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from blackholememory.observation_security import redact_secret_text
from blackholememory.repository_index import RepositoryIndexLimits
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import RepositoryWatcher
from blackholememory.repository_index import SQLiteRepositoryIndexStore
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state
from blackholememory.repository_index import repository_index_status


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("plan", "index", "watch", "status", "migrate"), default="plan")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--database", type=Path, default=ROOT / "runtime" / "live-memory" / "memories.sqlite3")
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--source-url", default="local://operator-owned")
    parser.add_argument("--license", dest="source_license", default="operator-owned")
    parser.add_argument("--evidence-class", default="E0")
    parser.add_argument("--owner", default="operator")
    parser.add_argument("--source-registry-id")
    parser.add_argument("--max-candidates", type=int, default=20_000)
    parser.add_argument("--max-file-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--max-files-per-run", type=int)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--feature-config", type=Path, default=ROOT / "config" / "cbm-integration.json")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--backup", type=Path)
    return parser.parse_args()


def _write_report(path: Path | None, payload: dict[str, object]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")


def _watch_feature_enabled(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    flags = payload.get("feature_flags", {})
    return flags.get("repository_watch_enabled") is True


def main() -> int:
    args = parse_args()
    limits = RepositoryIndexLimits(
        max_candidates=args.max_candidates,
        max_file_bytes=args.max_file_bytes,
        max_total_bytes=args.max_total_bytes,
        batch_size=args.batch_size,
    )
    source = RepositorySourceProvenance(
        source_url=args.source_url,
        license=args.source_license,
        evidence_class=args.evidence_class,
        owner=args.owner,
        source_registry_id=args.source_registry_id,
    )
    try:
        if args.action == "plan":
            state = probe_repository_state(args.root, project=args.project, limits=limits)
            current = None
            database = args.database.expanduser().resolve()
            store = SQLiteRepositoryIndexStore(database)
            repository_schema = store.inspect_schema()
            if repository_schema["ready"]:
                current = store.current_snapshot(
                    state.project,
                    state.root_id,
                    include_files=False,
                )
            result: dict[str, object] = {
                "schema_version": "bhm.repository-index.plan.v1",
                "ok": True,
                "action": "plan",
                "state": state.summary(),
                "repository_schema": repository_schema,
                "current_snapshot_id": current.get("snapshot_id") if current else None,
                "would_index": current is None or current.get("state_digest") != state.state_digest,
                "writes_sqlite_state": False,
            }
        elif args.action == "status":
            result = {
                "schema_version": "bhm.repository-index.status.v1",
                "ok": True,
                "action": "status",
                **repository_index_status(
                    args.root,
                    args.database,
                    project=args.project,
                    limits=limits,
                ),
                "writes_sqlite_state": False,
            }
        elif args.action == "migrate":
            if not args.confirm or args.backup is None:
                raise ValueError("migrate requires --confirm and --backup")
            result = {
                "action": "migrate",
                **SQLiteRepositoryIndexStore(args.database).migrate_empty_v1_to_v2(args.backup),
            }
        elif args.action == "index":
            if not args.confirm:
                raise ValueError("index requires --confirm")
            result = {
                "action": "index",
                **index_repository(
                    args.root,
                    args.database,
                    project=args.project,
                    limits=limits,
                    source=source,
                    max_files_per_run=args.max_files_per_run,
                ),
            }
        else:
            if not args.confirm:
                raise ValueError("watch requires --confirm")
            if args.cycles > 1 and not _watch_feature_enabled(args.feature_config):
                raise ValueError("multi-cycle watch requires repository_watch_enabled=true")
            watcher = RepositoryWatcher(
                args.root,
                args.database,
                project=args.project,
                limits=limits,
                source=source,
            )
            result = {"action": "watch", **watcher.run(cycles=args.cycles, interval_seconds=args.interval_seconds)}
        _write_report(args.report, result)
        return 0 if result.get("ok") else 1
    except (OSError, ValueError, RuntimeError) as exc:
        safe_error = redact_secret_text(str(exc)).value[:1_000]
        result = {
            "schema_version": "bhm.repository-index.error.v1",
            "ok": False,
            "action": args.action,
            "error": safe_error,
            "writes_memory_rows": False,
            "writes_qdrant": False,
        }
        _write_report(args.report, result)
        return 1


if __name__ == "__main__":
    sys.exit(main())
