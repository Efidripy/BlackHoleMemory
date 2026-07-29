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


TASK_ID = "TASK-20260717-BHM-UPSTREAM-PERMISSIONS"
PLAN_ID = "BHM-V5-POST-ACCEPTANCE-20260717"
LEDGER_SCHEMA = "bhm.p21.13.wi31.permission-attestation-ledger.v1"
TODAY = date(2026, 7, 21).isoformat()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
    if not isinstance(sources, list) or len(sources) != 33:
        raise ValueError(f"expected 33 registry sources, found {len(sources or [])}")
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
    by_id = {entry["source_id"]: entry for entry in ledger["entries"]}
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
    registry_path = repo / "config" / "source-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    source_root = repo / ".src"
    for source in registry["sources"]:
        manifest_path = source_root / str(source["slug"]) / "SOURCE-MANIFEST.json"
        if not manifest_path.is_file():
            continue
        manifest = _load(manifest_path)
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
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--operator-attested", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.operator_attested:
        raise SystemExit("refusing to compile permission ledger without --operator-attested")
    repo = args.repo_root.resolve()
    registry_path = repo / "config" / "source-registry.json"
    registry = _load(registry_path)
    ledger = compile_ledger(registry)
    ledger_path = repo / "docs" / "ops" / "bhm-p21.13-wi31-permission-attestation-ledger-2026-07-21.json"
    if args.apply:
        apply_metadata(registry, ledger, repo)
        ledger["registry_mutated"] = True
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"mode={'applied' if args.apply else 'check-only'} entries={len(ledger['entries'])} ledger={ledger_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
