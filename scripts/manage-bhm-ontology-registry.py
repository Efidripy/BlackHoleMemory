#!/usr/bin/env python
"""Dry-run-first local operator control for BHM ontology registry activation."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blackholememory.ontology_registry import ACTIVATION_ARTIFACT_TYPE
from blackholememory.ontology_registry import ARTIFACT_TYPE
from blackholememory.ontology_registry import OntologyRegistryError
from blackholememory.ontology_registry import OntologySchema
from blackholememory.ontology_registry import build_activation_artifact
from blackholememory.ontology_registry import build_registry_artifact
from blackholememory.runtime_storage import resolve_runtime_storage_config
from blackholememory.memory_service import SQLiteMemoryService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_schema(path: Path) -> OntologySchema:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OntologyRegistryError(f"schema file is invalid: {path}") from exc
    return OntologySchema.model_validate(raw)


def _service(runtime_dir: Path) -> SQLiteMemoryService:
    config = resolve_runtime_storage_config(runtime_dir=runtime_dir)
    return SQLiteMemoryService(config.database_path)


def _plan_activate(schema: OntologySchema) -> dict[str, Any]:
    if schema.activation_status not in {"declared", "active"}:
        raise OntologyRegistryError("only declared or active schemas can be activated")
    timestamp = _now()
    registry = build_registry_artifact(schema)
    marker = build_activation_artifact(schema, enabled=True, updated_at=timestamp)
    return {
        "schema_version": "bhm.ontology-activation-plan.v1",
        "action": "activate",
        "project": schema.project,
        "schema_digest": schema.digest(),
        "registry_artifact": registry.to_record(),
        "activation_artifact": marker.to_record(),
        "sqlite_mutation": True,
        "qdrant_mutation": False,
        "rollback": {"command": "--disable --project " + schema.project},
    }


def _find_schema_for_disable(service: SQLiteMemoryService, project: str) -> OntologySchema:
    marker_id = f"ontology_activation_{project}"
    markers = service.list_artifact_records(
        artifact_type=ACTIVATION_ARTIFACT_TYPE,
        project=project,
        limit=8,
    )
    marker = next((item for item in markers if item.get("id") == marker_id), None)
    if not isinstance(marker, dict) or not marker.get("registry_artifact_id"):
        raise OntologyRegistryError("no activation marker exists for project")
    registries = service.list_artifact_records(
        artifact_type=ARTIFACT_TYPE,
        project=project,
        limit=128,
    )
    record = next((item for item in registries if item.get("id") == marker["registry_artifact_id"]), None)
    if not isinstance(record, dict) or not isinstance(record.get("schema"), dict):
        raise OntologyRegistryError("activation marker does not resolve a registry schema")
    return OntologySchema.model_validate(record["schema"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path(".runtime"))
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--project")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.disable == (args.schema is not None):
        parser.error("specify exactly one of --schema or --disable")
    if args.disable and not args.project:
        parser.error("--disable requires --project")

    try:
        runtime_dir = args.runtime_dir.resolve()
        if args.disable:
            service = _service(runtime_dir)
            schema = _find_schema_for_disable(service, str(args.project))
            marker = build_activation_artifact(schema, enabled=False, updated_at=_now())
            plan = {
                "schema_version": "bhm.ontology-activation-plan.v1",
                "action": "disable",
                "project": schema.project,
                "schema_digest": schema.digest(),
                "activation_artifact": marker.to_record(),
                "sqlite_mutation": True,
                "qdrant_mutation": False,
            }
        else:
            schema = _load_schema(args.schema.resolve())
            plan = _plan_activate(schema)

        if args.apply:
            service = _service(runtime_dir)
            if plan["action"] == "activate":
                registry = build_registry_artifact(schema)
                service.save_artifact(registry)
            marker = build_activation_artifact(
                schema,
                enabled=plan["action"] == "activate",
                updated_at=_now(),
            )
            service.save_artifact(marker)
            plan["applied"] = True
        else:
            plan["applied"] = False
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (OntologyRegistryError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
