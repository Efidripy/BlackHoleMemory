#!/usr/bin/env python3
"""WI-01 deterministic watcher/index/crash-resume exit validator."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.repository_index import RepositoryIndexInjectedFailure
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import RepositoryWatcher
from blackholememory.repository_index import SQLiteRepositoryIndexStore
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import verify_repository_snapshot
from blackholememory.source_registry import load_registry


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _fixture(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "assets").mkdir()
    for index in range(8):
        (root / "src" / f"module_{index}.py").write_text(
            f"def symbol_{index}():\n    return {index}\n",
            encoding="utf-8",
        )
    (root / "tests" / "test_module.py").write_text(
        "from src.module_0 import symbol_0\n\ndef test_symbol():\n    assert symbol_0() == 0\n",
        encoding="utf-8",
    )
    (root / "docs" / "architecture.md").write_text("# Architecture\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=synthetic\n", encoding="utf-8")
    (root / "assets" / "binary.txt").write_bytes(b"text\x00binary")
    _git(root, "init")
    _git(root, "config", "user.email", "validator@example.invalid")
    _git(root, "config", "user.name", "Validator")
    _git(root, "add", "-f", ".")
    _git(root, "commit", "-m", "validator fixture")


def _tables(database: Path) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}
    source = RepositorySourceProvenance(
        source_url="fixture://wi01-validator",
        license="synthetic fixture",
        evidence_class="E0",
        owner="Codex /root",
        source_registry_id="WI01-FIXTURE",
    )
    try:
        integration = json.loads((ROOT / "config" / "cbm-integration.json").read_text(encoding="utf-8"))
        flags = integration["feature_flags"]
        forbidden = {"source_import_enabled", "migration_enabled", "obsidian_bridge_enabled", "autonomous_apply_enabled", "training_enabled", "lora_enabled"}
        checks["live_flags_remain_off"] = not any(bool(flags.get(name)) for name in forbidden)
        registry = load_registry(ROOT / "config" / "source-registry.json")
        checks["source_registry_clean_room"] = all(
            source_item["code_copy_allowed"] is False
            or (
                source_item.get("code_copy_allowed") is True
                and source_item.get("transfer_mode") == "direct-transfer-scoped"
                and source_item.get("permission_status") == "written-permission"
                and bool(source_item.get("covered_files"))
            )
            for source_item in registry["sources"]
        )
        live_schema = SQLiteRepositoryIndexStore(
            ROOT / "runtime" / "live-memory" / "memories.sqlite3"
        ).inspect_schema()
        checks["live_schema_ready_and_unpublished"] = live_schema["ready"] is True and live_schema.get("schema_version") == 2

        with tempfile.TemporaryDirectory(prefix="bhm-wi01-validator-") as raw:
            temp = Path(raw)
            root = temp / "repo"
            root.mkdir()
            _fixture(root)
            database = temp / "memories.sqlite3"
            SQLiteMemoryRepository(database).initialize()

            cold = index_repository(root, database, project="validator", source=source)
            store = SQLiteRepositoryIndexStore(database)
            cold_snapshot = store.snapshot(cold["snapshot_id"], include_files=True)
            checks["cold_snapshot_complete"] = cold["status"] == "completed" and cold["ok"] is True
            checks["snapshot_checksum"] = verify_repository_snapshot(cold_snapshot)
            checks["canonical_sqlite_coexistence"] = {
                "memories",
                "memory_outbox",
                "repository_index_snapshots",
                "repository_source_imports",
            }.issubset(_tables(database))
            skip_reasons = cold_snapshot["summary"]["skip_reasons"]
            checks["secret_binary_exclusions"] = (
                skip_reasons.get("secret-path") == 1 and skip_reasons.get("binary") == 1
            )

            resume_database = temp / "resume.sqlite3"
            partial = index_repository(
                root,
                resume_database,
                project="validator",
                source=source,
                max_files_per_run=3,
            )
            resumed = index_repository(root, resume_database, project="validator", source=source)
            checks["crash_resume"] = (
                partial["status"] == "running"
                and resumed["status"] == "completed"
                and resumed["metrics"]["resumed"] is True
                and resumed["snapshot"]["graph_input_digest"] == cold["snapshot"]["graph_input_digest"]
            )

            previous_id = cold["snapshot_id"]
            (root / "src" / "module_0.py").write_text("def symbol_0():\n    return 100\n", encoding="utf-8")
            (root / "src" / "module_1.py").rename(root / "src" / "renamed_module.py")
            (root / "docs" / "architecture.md").unlink()
            (root / "src" / "added.py").write_text("VALUE = 1\n", encoding="utf-8")
            try:
                index_repository(
                    root,
                    database,
                    project="validator",
                    source=source,
                    fail_before_publish=True,
                )
            except RepositoryIndexInjectedFailure:
                injected = True
            else:
                injected = False
            current_after_failure = store.current_snapshot("validator", cold["state"]["root_id"])
            checks["last_known_good_on_failure"] = (
                injected and current_after_failure is not None and current_after_failure["snapshot_id"] == previous_id
            )

            incremental = index_repository(root, database, project="validator", source=source)
            delta = incremental["snapshot"]["delta"]
            checks["incremental_delta"] = (
                delta["changed"] == ["src/module_0.py"]
                and delta["added"] == ["src/added.py"]
                and delta["removed"] == ["docs/architecture.md"]
                and len(delta["renamed"]) == 1
            )
            checks["unchanged_reuse"] = incremental["metrics"]["reused_unchanged_files"] >= 1
            watcher = RepositoryWatcher(root, database, project="validator", source=source)
            checks["freshness_and_no_daemon"] = (
                watcher.poll()["changed"] is False
                and watcher.run(cycles=1, interval_seconds=0, index_on_change=False)["starts_background_daemon"] is False
            )
            final_snapshot = store.snapshot(incremental["snapshot_id"], include_files=True)
            checks["source_import_provenance"] = (
                final_snapshot["source"]["source_registry_id"] == "WI01-FIXTURE"
                and final_snapshot["source"]["owner"] == "Codex /root"
            )
            connection = sqlite3.connect(database)
            try:
                file_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(repository_index_snapshot_files)"
                    ).fetchall()
                }
            finally:
                connection.close()
            checks["no_raw_source_persistence"] = "content" not in file_columns and "content_sha256" in file_columns
            cli_script = ROOT / "scripts" / "bhm-repository-index.py"
            cli_database = temp / "cli.sqlite3"
            denied = subprocess.run(
                [
                    sys.executable,
                    str(cli_script),
                    "--action",
                    "index",
                    "--root",
                    str(root),
                    "--database",
                    str(cli_database),
                    "--project",
                    "validator-cli",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            allowed = subprocess.run(
                [
                    sys.executable,
                    str(cli_script),
                    "--action",
                    "index",
                    "--root",
                    str(root),
                    "--database",
                    str(cli_database),
                    "--project",
                    "validator-cli",
                    "--confirm",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            denied_watch = subprocess.run(
                [
                    sys.executable,
                    str(cli_script),
                    "--action",
                    "watch",
                    "--root",
                    str(root),
                    "--database",
                    str(cli_database),
                    "--project",
                    "validator-cli",
                    "--cycles",
                    "2",
                    "--interval-seconds",
                    "0",
                    "--confirm",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            checks["cli_confirm_gate"] = denied.returncode == 1 and "requires --confirm" in denied.stdout
            checks["cli_index_smoke"] = allowed.returncode == 0 and json.loads(allowed.stdout)["ok"] is True
            denied_watch_payload = json.loads(denied_watch.stdout) if denied_watch.stdout.strip().startswith("{") else {}
            checks["multi_cycle_watch_flag_gate"] = (
                denied_watch.returncode == 0
                and denied_watch_payload.get("ok") is True
                and denied_watch_payload.get("starts_background_daemon") is False
            )
            details = {
                "cold_snapshot_id": cold["snapshot_id"],
                "incremental_snapshot_id": incremental["snapshot_id"],
                "graph_input_digest": incremental["snapshot"]["graph_input_digest"],
                "delta": delta,
                "reused_unchanged_files": incremental["metrics"]["reused_unchanged_files"],
                "skip_reasons": skip_reasons,
                "table_count": len(_tables(database)),
                "live_repository_schema_version": live_schema["schema_version"],
            }
        result = {
            "schema_version": "bhm.wi01.repository-index-validation.v1",
            "ok": all(checks.values()),
            "check_count": len(checks),
            "passed_count": sum(checks.values()),
            "checks": checks,
            "details": details,
            "writes_live_state": False,
            "writes_qdrant": False,
            "model_started": False,
        }
    except Exception as exc:
        result = {
            "schema_version": "bhm.wi01.repository-index-validation.v1",
            "ok": False,
            "checks": checks,
            "error": f"{type(exc).__name__}: {exc}",
            "writes_live_state": False,
        }
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
