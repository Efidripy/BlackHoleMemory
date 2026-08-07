#!/usr/bin/env python3
"""Validate bounded operator-managed watcher backpressure (WI-83).

The validator uses an isolated temporary repository/database.  It proves that
watcher pressure is read-only until an explicit operator ``run`` call, that a
different running job blocks a new index, and that a same-state crash resume
remains allowed.  No daemon, Qdrant write, raw source persistence, or live
project mutation is involved.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import RepositoryWatcher
from blackholememory.repository_index import SQLiteRepositoryIndexStore
from blackholememory.repository_index import probe_repository_state
from blackholememory.filesystem_boundaries import replace_bytes_safely


def _fixture(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "module.py").write_text(
        "def value():\n    return 1\n", encoding="utf-8"
    )


def _source() -> RepositorySourceProvenance:
    return RepositorySourceProvenance(
        source_url="local://wi83-validator",
        license="operator-owned",
        evidence_class="E0",
        owner="validator",
    )


def build_report() -> dict[str, Any]:
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="bhm-p28-wi83-") as temporary:
        workspace = Path(temporary)
        root = workspace / "repo"
        database = workspace / "runtime" / "index.sqlite3"
        _fixture(root)
        source = _source()
        watcher = RepositoryWatcher(root, database, project="wi83", source=source)

        # Poll/pressure are explicitly read-only and must not create SQLite.
        pressure_before = watcher.backpressure()
        checks["pressure_probe_is_read_only"] = (
            pressure_before["active_job_count"] == 0
            and pressure_before["writes_sqlite_state"] is False
            and not database.exists()
        )

        state = probe_repository_state(root, project="wi83")
        store = SQLiteRepositoryIndexStore(database)
        running = store.begin_or_resume_job(state, source)
        (root / "src" / "module.py").write_text(
            "def value():\n    return 2\n", encoding="utf-8"
        )
        pressure_blocked = watcher.backpressure()
        checks["different_state_is_backpressured"] = (
            pressure_blocked["blocked"] is True
            and pressure_blocked["blocking_job_count"] == 1
            and pressure_blocked["operator_managed"] is True
            and pressure_blocked["autonomous_apply"] is False
        )
        blocked_run = watcher.run(cycles=1, interval_seconds=0)
        blocked_event = blocked_run["events"][0]
        checks["blocked_run_does_not_index"] = (
            blocked_run["backpressured_cycles"] == 1
            and blocked_event.get("backpressured") is True
            and blocked_event.get("index") is None
            and store.job(running["job_id"])["status"] == "running"
            and store.current_snapshot("wi83", state.root_id) is None
        )

        # A fresh exact-state running job is resumable, not a pressure blocker.
        resume_root = workspace / "resume-repo"
        resume_database = workspace / "resume-runtime" / "index.sqlite3"
        _fixture(resume_root)
        resumed = RepositoryWatcher(resume_root, resume_database, project="wi83", source=source)
        resume_state = probe_repository_state(resume_root, project="wi83")
        resume_store = SQLiteRepositoryIndexStore(resume_database)
        resume_job = resume_store.begin_or_resume_job(resume_state, source)
        same_state = resumed.backpressure()
        checks["same_state_resume_is_allowed"] = (
            same_state["blocked"] is False
            and same_state["blocking_job_count"] == 0
        )

        resumed_run = resumed.run(cycles=1, interval_seconds=0)
        checks["explicit_resume_completes"] = (
            resumed_run["ok"] is True
            and resumed_run["backpressured_cycles"] == 0
            and resumed_run["events"][0].get("index", {}).get("job_id") == resume_job["job_id"]
        )
        checks["no_daemon_or_projection_write"] = all(
            event.get("poll", {}).get("starts_background_daemon") is False
            and event.get("backpressure", {}).get("starts_background_daemon") is False
            and event.get("backpressure", {}).get("writes_qdrant") is False
            for event in blocked_run["events"] + resumed_run["events"]
        )
        checks["bounded_limit"] = False
        try:
            RepositoryWatcher(root, database, project="wi83", max_inflight_jobs=5)
        except ValueError:
            checks["bounded_limit"] = True

    failures = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema_version": "bhm.p28.wi83-watch-backpressure.v1",
        "ok": not failures,
        "checks": checks,
        "failures": failures,
        "operator_managed": True,
        "starts_background_daemon": False,
        "autonomous_apply": False,
        "writes_qdrant": False,
        "raw_source_returned": False,
        "writes_sqlite_state": "explicit operator run only",
        "rollback": "stop the operator run; leave the running job/checkpoint for explicit resume or restore the hash-verified SQLite backup; no Qdrant projection is changed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = build_report()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        replace_bytes_safely(args.report, (rendered + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
