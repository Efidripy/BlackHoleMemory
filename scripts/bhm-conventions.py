"""Explicit WI-04 conventions and architecture-memory operator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.convention_memory import CONVENTION_SCHEMA_VERSION
from blackholememory.convention_memory import ConventionMemoryError
from blackholememory.convention_memory import SQLiteConventionMemoryStore
from blackholememory.convention_memory import build_convention_memory
from blackholememory.convention_memory import explain_convention_card
from blackholememory.convention_memory import preview_convention_memory
from blackholememory.repository_index import SQLiteRepositoryIndexStore
from blackholememory.repository_index import probe_repository_state
from blackholememory.code_graph import SQLiteCodeGraphStore


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = REPO_ROOT / "runtime" / "live-memory" / "memories.sqlite3"
DEFAULT_FEATURE_CONFIG = REPO_ROOT / "config" / "cbm-integration.json"


def _emit(value: object, report: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(rendered)
    if report:
        target = Path(report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")


def _config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _base(args: argparse.Namespace) -> tuple[Path, Path, str, str]:
    root = Path(args.root).expanduser().resolve() if args.root else REPO_ROOT
    database = Path(args.database).expanduser().resolve() if args.database else DEFAULT_DATABASE
    project = str(args.project or root.name).strip().casefold()
    state = probe_repository_state(root, project=project)
    return root, database, project, str(state.root_id)


def _live_guard(args: argparse.Namespace, database: Path) -> None:
    if database != DEFAULT_DATABASE:
        return
    if not args.allow_live:
        raise ConventionMemoryError("live convention build/review requires --allow-live; use an isolated evidence database")
    flags = _config(Path(args.feature_config)).get("feature_flags") or {}
    if not bool(flags.get("integration_enabled")) or not bool(flags.get("convention_memory_enabled")):
        raise ConventionMemoryError("live convention build/review requires integration_enabled=true and convention_memory_enabled=true")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "preview", "build", "status", "explain", "review"), default="plan")
    parser.add_argument("--root")
    parser.add_argument("--database")
    parser.add_argument("--project")
    parser.add_argument("--graph-snapshot-id")
    parser.add_argument("--card-id")
    parser.add_argument("--decision", choices=("proposal", "accepted", "rejected"))
    parser.add_argument("--reviewer", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--feature-config", default=str(DEFAULT_FEATURE_CONFIG))
    parser.add_argument("--report")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    try:
        root, database, project, root_id = _base(args)
        index_store = SQLiteRepositoryIndexStore(database)
        graph_store = SQLiteCodeGraphStore(database)
        convention_store = SQLiteConventionMemoryStore(database)
        if args.action == "plan":
            _emit({"schema_version": CONVENTION_SCHEMA_VERSION, "ok": True, "action": "plan", "root": str(root), "database_path": str(database), "project": project, "root_id": root_id, "repository_schema": index_store.inspect_schema(), "graph_schema": graph_store.inspect_schema(), "convention_schema": convention_store.inspect_schema(), "writes_sqlite_state": False, "writes_qdrant": False, "model_started": False}, args.report)
            return 0
        if args.action == "status":
            _emit({"schema_version": CONVENTION_SCHEMA_VERSION, "ok": True, "action": "status", "database_path": str(database), "project": project, "root_id": root_id, "convention_schema": convention_store.inspect_schema(), "current_conventions": convention_store.current_snapshot(project, root_id, include_cards=False), "writes_sqlite_state": False}, args.report)
            return 0
        if args.action == "preview":
            _emit(preview_convention_memory(database, project=project, root_id=root_id, graph_snapshot_id=args.graph_snapshot_id), args.report)
            return 0
        if args.action == "explain":
            if not args.card_id:
                raise ConventionMemoryError("--card-id is required for explain")
            _emit(explain_convention_card(database, project=project, root_id=root_id, card_id=args.card_id), args.report)
            return 0
        if not args.confirm:
            raise ConventionMemoryError(f"{args.action} requires --confirm")
        _live_guard(args, database)
        if args.action == "build":
            _emit(build_convention_memory(database, project=project, root_id=root_id, graph_snapshot_id=args.graph_snapshot_id), args.report)
            return 0
        if args.action == "review":
            if not args.card_id or not args.decision:
                raise ConventionMemoryError("review requires --card-id and --decision")
            _emit(convention_store.review_card(project=project, root_id=root_id, card_id=args.card_id, decision=args.decision, reviewer=args.reviewer, reason=args.reason), args.report)
            return 0
        raise ConventionMemoryError(f"unsupported action: {args.action}")
    except (ConventionMemoryError, OSError, ValueError) as exc:
        _emit({"schema_version": CONVENTION_SCHEMA_VERSION, "ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]}, args.report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
