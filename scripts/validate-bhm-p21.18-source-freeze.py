#!/usr/bin/env python3
"""Validate the post-adoption source freeze and quarantine boundary."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def git(*args: str) -> list[str]:
    env = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
    result = subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8", env=env)
    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    registry = json.loads(Path("config/source-registry.json").read_text(encoding="utf-8"))
    sources = registry.get("sources", [])
    registry_ids = {item.get("id") for item in sources}
    manifests = sorted(Path(".src").glob("*/SOURCE-MANIFEST.json"))
    manifest_data = [json.loads(path.read_text(encoding="utf-8")) for path in manifests]
    delta = json.loads(Path(".docs/ops/bhm-p21.17-wi35-source-delta-2026-07-21.json").read_text(encoding="utf-8"))
    cbm = json.loads(Path("config/cbm-integration.json").read_text(encoding="utf-8"))
    flags = cbm.get("feature_flags", {})
    tracked = git("ls-files", "--", ".src")
    staged = git("diff", "--cached", "--name-only", "--", ".src")
    visible_untracked = git("ls-files", "--others", "--exclude-standard", "--", ".src")
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8")
    failures = []
    if len(sources) != 33:
        failures.append(f"registry source count={len(sources)}")
    registry_manifests = [item for item in manifest_data if item.get("source_id") in registry_ids]
    auxiliary_manifests = [item for item in manifest_data if item.get("source_id") not in registry_ids]
    if len(registry_manifests) != len(sources):
        failures.append(f"registry manifest count={len(registry_manifests)}")
    if tracked or staged or visible_untracked:
        failures.append(".src tracked/staged/visible residue present")
    if ".src/" not in gitignore or ".src/" not in dockerignore:
        failures.append(".src ignore boundary missing")
    if delta.get("adopted_delta_count") != 0 or delta.get("runtime_dependency_count") != 0:
        failures.append("source-delta ledger contains an adopted/runtime dependency")
    if any(
        (
            item.get("code_copy_allowed") is not False
            and not (
                item.get("code_copy_allowed") is True
                and item.get("transfer_mode") == "direct-transfer-scoped"
                and item.get("permission_status") == "written-permission"
                and bool(item.get("covered_files"))
            )
        )
        or item.get("runtime_dependency") is not False
        or item.get("authoritative_bhm_state") is not False
        for item in registry_manifests
    ):
        failures.append("manifest policy boundary violated")
    if any(not item.get("purpose") or not item.get("disposition") or item.get("material_present") is True for item in auxiliary_manifests):
        failures.append("auxiliary manifest lacks provenance or contains material")
    if any(bool(value) for key, value in flags.items() if key.endswith("_enabled") and key in {"source_import_enabled", "autonomous_apply_enabled", "training_enabled", "lora_enabled"}):
        failures.append("unsafe source/apply/training flag enabled")
    report = {
        "schema_version": "bhm.p21.18.wi36.source-freeze.v1",
        "generated_at": "2026-07-21",
        "plan_id": "BHM-V5-POST-ACCEPTANCE-20260717",
        "registry_count": len(sources),
        "manifest_count": len(manifests),
        "registry_manifest_count": len(registry_manifests),
        "auxiliary_manifest_count": len(auxiliary_manifests),
        "tracked_src": len(tracked),
        "staged_src": len(staged),
        "visible_untracked_src": len(visible_untracked),
        "ignore_boundary": {"git": ".src/" in gitignore, "docker": ".src/" in dockerignore},
        "source_delta_adopted": delta.get("adopted_delta_count"),
        "runtime_dependency_count": delta.get("runtime_dependency_count"),
        "unsafe_flags": {key: value for key, value in flags.items() if key in {"source_import_enabled", "autonomous_apply_enabled", "training_enabled", "lora_enabled"}},
        "freeze_mode": "no-adoption-delta; retain quarantine evidence and freeze additions",
        "writes_live_state": False,
        "failures": failures,
        "ok": not failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "registry": len(sources), "manifests": len(manifests), "tracked": len(tracked), "staged": len(staged), "failures": failures}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
