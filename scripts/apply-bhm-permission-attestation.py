#!/usr/bin/env python3
"""Compile and optionally apply the sanitized upstream-permission attestation.

Private correspondence is never read or persisted.  The operator must pass
``--operator-attested`` explicitly; the generated ledger binds each registry
source to its exact revision and existing allowed-use scope while preserving
the deny-by-default executable-copy boundary.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import replace_bytes_safely


TASK_ID = "TASK-20260717-BHM-UPSTREAM-PERMISSIONS"
PLAN_ID = "BHM-V5-POST-ACCEPTANCE-20260717"
LEDGER_SCHEMA = "bhm.p21.13.wi31.permission-attestation-ledger.v1"
TODAY = date(2026, 7, 21).isoformat()


def _load(path: Path) -> dict[str, Any]:
    safe_path = assert_safe_path(path)
    return json.loads(safe_path.read_text(encoding="utf-8"))


def _source_manifest_path(source_root: Path, slug: str) -> Path:
    """Resolve one registry slug without allowing traversal or reparse escapes."""

    normalized = str(slug or "").strip().replace("\\", "/")
    slug_path = Path(normalized)
    if (
        not normalized
        or normalized in {".", ".."}
        or slug_path.is_absolute()
        or slug_path.name != normalized
        or "/" in normalized
        or ".." in slug_path.parts
    ):
        raise ValueError(f"unsafe source slug: {slug!r}")
    return assert_safe_path(source_root / normalized / "SOURCE-MANIFEST.json")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    replace_bytes_safely(path, payload)


def _entry(source: dict[str, Any]) -> dict[str, Any]:
    source_id = str(source["id"])
    return {
        "source_id": source_id,
        "slug": source["slug"],
        "source_url": source["source_url"],
        "exact_revision": source["revision"],
        "permission_status": "written-permission",
        "permission_evidence_ref": f"attest:bhm-upstream-permissions:{source_id}:20260721",
        "rightsholder": source.get("attribution") or source.get("name"),
        "covered_scope": {
            "source_url": source["source_url"],
            "exact_revision": source["revision"],
            "allowed_use": source["allowed_use"],
            "code_transfer": "not-authorized-by-this-ledger",
        },
        "covered_files": [],
        "covered_capabilities": list(source.get("purpose", [])),
        "third_party_exclusions": [
            "third-party dependencies and notices",
            "embedded databases, .env, credentials, personal/live data",
            "external runtime/MCP authority",
            "unreviewed or unpinned source material",
        ],
        "permission_checked_at": TODAY,
        "code_copy_allowed": False,
        "reviewer": "Codex /root",
        "recheck_date": source["recheck_date"],
    }


def compile_ledger(registry: dict[str, Any]) -> dict[str, Any]:
    sources = registry.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("source registry must contain at least one source")
    source_ids = [str(source.get("id") or "") for source in sources if isinstance(source, dict)]
    if len(source_ids) != len(sources) or any(not source_id for source_id in source_ids):
        raise ValueError("source registry entries must be objects with non-empty ids")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("source registry source ids must be unique")
    return {
        "schema_version": LEDGER_SCHEMA,
        "generated_at": TODAY,
        "plan_id": PLAN_ID,
        "task_id": TASK_ID,
        "attestation_basis": "operator-confirmed written permission; private correspondence remains outside repository",
        "private_correspondence_storage": "forbidden",
        "scope_rule": "exact pinned source revision and existing registry allowed_use only",
        "code_copy_rule": "false until P21.14/P21.17 provenance, dependency, security, compatibility and rollback gates",
        "registry_mutated": False,
        "entries": [_entry(source) for source in sources],
    }


def apply_metadata(registry: dict[str, Any], ledger: dict[str, Any], repo: Path) -> None:
    repo = assert_safe_path(repo, reject_hardlink_target=False)
    by_id = {entry["source_id"]: entry for entry in ledger["entries"]}
    registry_path = assert_safe_path(repo / "config" / "source-registry.json")
    source_root = assert_safe_path(repo / ".src", reject_hardlink_target=False)
    manifest_targets: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
    for source in registry["sources"]:
        manifest_path = _source_manifest_path(source_root, str(source["slug"]))
        if not manifest_path.is_file():
            continue
        manifest = _load(manifest_path)
        manifest_targets.append((source, manifest_path, manifest))

    for source in registry["sources"]:
        entry = by_id[source["id"]]
        for key in (
            "permission_status",
            "permission_evidence_ref",
            "rightsholder",
            "covered_scope",
            "covered_files",
            "covered_capabilities",
            "third_party_exclusions",
            "permission_checked_at",
        ):
            source[key] = entry[key]
        source["code_copy_allowed"] = False

    registry["generated_at"] = TODAY
    registry["plan_id"] = PLAN_ID
    registry["attestation_ref"] = ".docs/ops/bhm-p21.13-wi31-permission-attestation-ledger-2026-07-21.json"
    _write_json(registry_path, registry)

    for source, manifest_path, manifest in manifest_targets:
        for key in (
            "permission_status",
            "permission_evidence_ref",
            "rightsholder",
            "covered_scope",
            "covered_files",
            "covered_capabilities",
            "third_party_exclusions",
            "permission_checked_at",
        ):
            manifest[key] = source[key]
        manifest["code_copy_allowed"] = False
        _write_json(manifest_path, manifest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--operator-attested", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.operator_attested:
        raise SystemExit("refusing to compile permission ledger without --operator-attested")
    repo = assert_safe_path(args.repo_root, reject_hardlink_target=False)
    if not repo.is_dir():
        raise SystemExit(f"repo root is not a directory: {repo}")
    registry_path = repo / "config" / "source-registry.json"
    registry = _load(registry_path)
    ledger = compile_ledger(registry)
    ledger_path = repo / "docs" / "ops" / "bhm-p21.13-wi31-permission-attestation-ledger-2026-07-21.json"
    if args.apply:
        apply_metadata(registry, ledger, repo)
        ledger["registry_mutated"] = True
    _write_json(repo / "docs" / "ops" / ledger_path.name, ledger)
    print(f"mode={'applied' if args.apply else 'check-only'} entries={len(ledger['entries'])} ledger={ledger_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
