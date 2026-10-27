"""Operator CLI for exact projection-generation binding migration and routing state.

Default actions are read-only.  Migration, apply, and rollback require explicit
confirmation and offline proof; the tool never writes Qdrant or Mem0.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blackholememory.projection_generation_binding import (
    active_projection_generation_bindings,
    apply_projection_generation_binding_plan,
    build_projection_generation_binding_plan,
    resolve_projection_route,
    rollback_projection_generation_binding,
)
from blackholememory.projection_generation_binding_migration import apply_projection_generation_binding_migration
from blackholememory.projection_generation_binding_migration import build_projection_generation_binding_migration_plan
from blackholememory.projection_generation_binding_migration import projection_generation_binding_migration_status


def _load_json(path: str) -> Any:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("status", "plan-migration", "migrate", "plan-bindings", "apply", "rollback"), default="status")
    parser.add_argument("--database", required=True)
    parser.add_argument("--backup")
    parser.add_argument("--as-of")
    parser.add_argument("--bindings-json")
    parser.add_argument("--plan-json")
    parser.add_argument("--actor-digest")
    parser.add_argument("--correlation-id")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--project")
    parser.add_argument("--binding-digest")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--offline-writer-verified", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "status":
            bindings = active_projection_generation_bindings(args.database)
            source_drift = any(
                resolve_projection_route(project=item.project, legacy_collection="", database=args.database, embedding_model=item.embedding_model, vector_dimensions=item.vector_dimensions, distance=item.distance).reason == "binding_active_source_drift"
                for item in bindings
            )
            _emit({"migration": projection_generation_binding_migration_status(args.database), "active_bindings": [item.to_dict() for item in bindings], "source_drift": source_drift})
            return 0
        if args.action == "plan-migration":
            if not args.backup or not args.as_of:
                raise ValueError("--backup and --as-of are required")
            _emit(build_projection_generation_binding_migration_plan(args.database, args.backup, as_of=args.as_of))
            return 0
        if args.action == "migrate":
            if not args.backup or not args.plan_json or not args.expected_plan_digest:
                raise ValueError("--backup, --plan-json and --expected-plan-digest are required")
            _emit(apply_projection_generation_binding_migration(args.database, args.backup, _load_json(args.plan_json), expected_plan_digest=args.expected_plan_digest, confirm_operator=args.confirm, offline_verified=args.offline_writer_verified))
            return 0
        if args.action == "plan-bindings":
            if not args.bindings_json or not args.as_of or not args.actor_digest or not args.correlation_id:
                raise ValueError("--bindings-json, --as-of, --actor-digest and --correlation-id are required")
            bindings = _load_json(args.bindings_json)
            if not isinstance(bindings, list):
                raise ValueError("--bindings-json must contain an array")
            if not args.database or not args.backup:
                raise ValueError("--database and --backup are required")
            _emit(build_projection_generation_binding_plan(bindings, database=args.database, existing_backup=args.backup, as_of=args.as_of, actor_digest=args.actor_digest, correlation_id=args.correlation_id))
            return 0
        if args.action == "apply":
            if not args.plan_json or not args.expected_plan_digest:
                raise ValueError("--plan-json and --expected-plan-digest are required")
            if not args.backup:
                raise ValueError("--backup is required")
            _emit(apply_projection_generation_binding_plan(args.database, args.backup, _load_json(args.plan_json), expected_plan_digest=args.expected_plan_digest, confirm_operator=args.confirm, offline_verified=args.offline_writer_verified))
            return 0
        if not args.project or not args.binding_digest or not args.actor_digest or not args.correlation_id:
            raise ValueError("--project, --binding-digest, --actor-digest and --correlation-id are required")
        if not args.backup:
            raise ValueError("--backup is required")
        _emit(rollback_projection_generation_binding(args.database, args.backup, project=args.project, binding_digest=args.binding_digest, actor_digest=args.actor_digest, correlation_id=args.correlation_id, confirm_operator=args.confirm, offline_verified=args.offline_writer_verified))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        _emit({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
