#!/usr/bin/env python3
"""Bounded live CBM workflow proof for one local repository.

The target repository is read-only. All BHM state is written to a disposable
SQLite database supplied by ``--database``; no source, Git or Qdrant mutation is
performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.code_graph import SQLiteCodeGraphStore, build_code_graph
from blackholememory.code_graph_query import explain_code_graph, query_code_graph
from blackholememory.convention_memory import preview_convention_memory
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import RepositoryWatcher, index_repository, probe_repository_state
from blackholememory.unified_context import build_unified_context_from_graph


REPO_ROOT = Path(__file__).resolve().parents[1]
GIT_PROBE_TIMEOUT_SECONDS = PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS
DISPOSABLE_DATABASE_ROOT = REPO_ROOT / ".runtime" / "validation-databases"


def approved_database_path(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    root = DISPOSABLE_DATABASE_ROOT.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"database must be under approved disposable root: {candidate}") from exc
    return assert_safe_path(candidate)


def _write_report(path: Path, report: dict) -> None:
    replace_bytes_safely(
        path,
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GIT_PROBE_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project", default="bonsai-demo")
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _args()
    root = args.root.expanduser().resolve()
    database = approved_database_path(args.database)
    report_path = args.report.expanduser().resolve()
    before_status = _git(root, "status", "--porcelain=v1")
    source_url = _git(root, "remote", "get-url", "origin")
    revision = _git(root, "rev-parse", "HEAD")
    source = RepositorySourceProvenance(
        owner="PrismML-Eng/Bonsai-demo",
        source_url=source_url,
        license="Apache-2.0",
        evidence_class="E0",
        source_registry_id="LOCAL-BONSAI-DEMO-20260722",
    )
    checks: dict[str, bool] = {}
    details: dict[str, object] = {
        "root": str(root),
        "source_url": source_url,
        "revision": revision,
        "license": "Apache-2.0",
        "source_status_before": before_status,
    }

    first = index_repository(root, database, project=args.project, source=source)
    state = probe_repository_state(root, project=args.project)
    graph = build_code_graph(database, project=args.project, root_id=state.root_id, repository_snapshot_id=first["snapshot_id"])
    graph_store = SQLiteCodeGraphStore(database)
    graph_snapshot = graph_store.snapshot(graph["graph_snapshot_id"], include_material=True)
    checks["cold_index_completed"] = first["ok"] is True and first["status"] == "completed"
    checks["source_provenance_bound"] = first["snapshot"]["source"]["source_registry_id"] == source.source_registry_id
    checks["graph_completed_and_redacted"] = (
        graph["ok"] is True
        and graph["summary"]["node_count"] > 0
        and all("content" not in node and "raw_source" not in node for node in graph_snapshot["nodes"])
    )
    checks["graph_provenance_present"] = all(
        node.get("provenance", {}).get("extractor_version")
        for node in graph_snapshot["nodes"]
        if node.get("node_kind") not in {"external_module", "unresolved_symbol"}
    )

    symbol_query = query_code_graph(
        database,
        project=args.project,
        root_id=state.root_id,
        operation="symbol",
        query="start_llama_server",
        depth=2,
        limit=16,
        time_budget_ms=2_000,
    )
    explanation = explain_code_graph(
        database,
        project=args.project,
        root_id=state.root_id,
        operation="neighborhood",
        query="start_llama_server",
        depth=2,
        limit=16,
        time_budget_ms=2_000,
    )
    checks["query_explain_read_only"] = (
        symbol_query["execution"]["writes_sqlite_state"] is False
        and symbol_query["execution"]["raw_source_returned"] is False
        and explanation["execution"]["writes_sqlite_state"] is False
        and bool(explanation.get("explanations"))
    )

    conventions = preview_convention_memory(
        database,
        project=args.project,
        root_id=state.root_id,
        graph_snapshot_id=graph["graph_snapshot_id"],
    )
    checks["conventions_are_proposals"] = bool(conventions["cards"]) and all(
        card["status"] == "proposal" and card["review"]["decision"] == "proposal"
        for card in conventions["cards"]
    )
    unified = build_unified_context_from_graph(
        database,
        project=args.project,
        root_id=state.root_id,
        query="llama server",
        code_operation="symbol",
        include_code=True,
        include_conventions=True,
        include_proposals=False,
        token_budget=1_500,
        limit=12,
        time_budget_ms=2_000,
    )
    checks["unified_context_available"] = (
        bool(unified.get("response_digest"))
        and unified.get("retrieval", {}).get("include_code") is True
        and unified.get("retrieval", {}).get("include_conventions") is True
    )

    watcher = RepositoryWatcher(root, database, project=args.project, source=source)
    watcher_poll = watcher.poll()
    watcher_run = watcher.run(cycles=1, interval_seconds=0, index_on_change=False)
    checks["watcher_is_bounded_and_fresh"] = watcher_poll["changed"] is False and watcher_run["starts_background_daemon"] is False

    resume_database = database.with_name(database.stem + "-resume.sqlite3")
    partial = index_repository(root, resume_database, project=args.project, source=source, max_files_per_run=3)
    resumed = index_repository(root, resume_database, project=args.project, source=source)
    checks["restart_resume"] = (
        partial["status"] == "running"
        and resumed["status"] == "completed"
        and resumed["metrics"]["resumed"] is True
        and resumed["snapshot"]["graph_input_digest"] == first["snapshot"]["graph_input_digest"]
    )

    with tempfile.TemporaryDirectory(prefix="bhm-p23.1-copy-") as raw_copy:
        copy_root = Path(raw_copy) / root.name
        shutil.copytree(root, copy_root, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
        copy_database = database.with_name(database.stem + "-incremental.sqlite3")
        initial_copy = index_repository(copy_root, copy_database, project=args.project, source=source)
        marker = copy_root / "P23-CBM-SMOKE-MARKER.md"
        marker.write_text("temporary CBM smoke marker\n", encoding="utf-8")
        incremental = RepositoryWatcher(copy_root, copy_database, project=args.project, source=source).run(cycles=1, interval_seconds=0, index_on_change=True)
        event = incremental["events"][0]
        # Explicit cleanup proof: the temporary repository and all disposable
        # databases are removed by their owning scopes after the receipt is built.
        checks["incremental_update_and_cleanup"] = (
            initial_copy["ok"] is True
            and event["poll"]["changed"] is True
            and event["index"]["ok"] is True
        )

    after_status = _git(root, "status", "--porcelain=v1")
    checks["target_repository_unchanged"] = before_status == after_status
    details.update(
        {
            "snapshot_id": first["snapshot_id"],
            "graph_snapshot_id": graph["graph_snapshot_id"],
            "graph_digest": graph["graph_digest"],
            "node_count": graph["summary"]["node_count"],
            "edge_count": graph["summary"]["edge_count"],
            "convention_snapshot_id": conventions.get("convention_snapshot_id"),
            "convention_digest": conventions.get("convention_digest"),
            "convention_card_count": len(conventions["cards"]),
            "query_response_digest": symbol_query.get("response_digest"),
            "explain_response_digest": explanation.get("response_digest"),
            "unified_response_digest": unified.get("response_digest"),
            "watcher": watcher_run,
            "resume": {"partial_status": partial["status"], "resumed": resumed["metrics"]["resumed"]},
            "target_tree_sha256": _sha256(root / "README.md"),
        }
    )
    failed = sorted(name for name, ok in checks.items() if not ok)
    report = {
        "schema_version": "bhm.p23.1.small-repo-live-proof.v1",
        "ok": not failed,
        "checks": checks,
        "failed": failed,
        "details": details,
        "writes_selected_repository": False,
        "writes_qdrant": False,
        "model_started": False,
        "disposition": "pass" if not failed else "blocked",
    }
    _write_report(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
