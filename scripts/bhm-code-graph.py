"""Explicit WI-02 canonical code graph operator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.code_graph import CODE_GRAPH_SCHEMA_VERSION
from blackholememory.code_graph import CodeGraphError
from blackholememory.code_graph import SQLiteCodeGraphStore
from blackholememory.code_graph import build_code_graph
from blackholememory.repository_index import SQLiteRepositoryIndexStore
from blackholememory.repository_index import probe_repository_state


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = REPO_ROOT / "runtime" / "live-memory" / "memories.sqlite3"
DEFAULT_FEATURE_CONFIG = REPO_ROOT / "config" / "cbm-integration.json"


def _json(value: object, report: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(rendered)
    if report:
        output = Path(report).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")


def _config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _base(args: argparse.Namespace) -> tuple[Path, Path, str, str]:
    root = Path(args.root).expanduser().resolve() if args.root else REPO_ROOT
    database = Path(args.database).expanduser().resolve() if args.database else DEFAULT_DATABASE
    project = str(args.project or root.name).strip().casefold()
    state = probe_repository_state(root, project=project)
    return root, database, project, str(state.root_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "build", "status"), default="plan")
    parser.add_argument("--root")
    parser.add_argument("--database")
    parser.add_argument("--project")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--feature-config", default=str(DEFAULT_FEATURE_CONFIG))
    parser.add_argument("--report")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    try:
        root, database, project, root_id = _base(args)
        index_store = SQLiteRepositoryIndexStore(database)
        graph_store = SQLiteCodeGraphStore(database)
        if args.action == "plan":
            repository = index_store.current_snapshot(project, root_id, include_files=False)
            _json({"schema_version": CODE_GRAPH_SCHEMA_VERSION, "ok": True, "action": "plan", "root": str(root), "database_path": str(database), "project": project, "root_id": root_id, "repository_snapshot_id": repository.get("snapshot_id") if repository else None, "repository_schema": index_store.inspect_schema(), "graph_schema": graph_store.inspect_schema(), "writes_sqlite_state": False, "writes_qdrant": False, "model_started": False}, args.report)
            return 0
        if args.action == "status":
            _json({"schema_version": CODE_GRAPH_SCHEMA_VERSION, "ok": True, "action": "status", "database_path": str(database), "project": project, "root_id": root_id, "graph_schema": graph_store.inspect_schema(), "current_graph": graph_store.current_snapshot(project, root_id, include_material=False), "writes_sqlite_state": False}, args.report)
            return 0
        if not args.confirm:
            raise CodeGraphError("build requires --confirm")
        if database == DEFAULT_DATABASE and not args.allow_live:
            raise CodeGraphError("live database build requires --allow-live; use an isolated evidence database for WI-02")
        if database == DEFAULT_DATABASE and args.allow_live:
            flags = _config(Path(args.feature_config)).get("feature_flags") or {}
            if not bool(flags.get("integration_enabled")) or not bool(flags.get("code_index_enabled")):
                raise CodeGraphError("live graph build requires integration_enabled=true and code_index_enabled=true")
        result = build_code_graph(database, project=project, root_id=root_id, repository_snapshot_id=args.snapshot_id)
        _json(result, args.report)
        return 0
    except (CodeGraphError, OSError, ValueError) as exc:
        _json({"schema_version": CODE_GRAPH_SCHEMA_VERSION, "ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]}, args.report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
