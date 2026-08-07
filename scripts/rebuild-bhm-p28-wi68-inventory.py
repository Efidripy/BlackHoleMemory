"""Rebuild the deterministic WI-68 component inventory from the pinned quarantine tree."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".src" / "codebase-memory-mcp" / "source"
MANIFEST = ROOT / ".src" / "codebase-memory-mcp" / "SOURCE-MANIFEST.json"
OUT = ROOT / "docs" / "ops" / "bhm-p28-wi68-component-license-sbom-inventory-2026-07-23.json"
GIT_PROBE_TIMEOUT_SECONDS = 30


class GitInventoryError(RuntimeError):
    """Raised when the read-only Git inventory probe cannot complete."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def tracked_paths() -> tuple[set[str], set[str]]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(SOURCE), "ls-files"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitInventoryError("git source inventory probe unavailable") from exc
    rows = completed.stdout.splitlines()
    regular: set[str] = set()
    symlinks: set[str] = set()
    for row in rows:
        if row.startswith(".git/"):
            continue
        path = SOURCE / row
        if path.is_symlink():
            symlinks.add(row)
        elif path.is_file():
            regular.add(row)
    return regular, symlinks


def tree_digest(paths: set[str]) -> str:
    digest = hashlib.sha256()
    for row in sorted(paths):
        digest.update(row.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(SOURCE / row)))
        digest.update(b"\n")
    return digest.hexdigest()


def select(paths: set[str], globs: list[str]) -> set[str]:
    return {
        row
        for row in paths
        if any(row == glob or (glob.endswith("/**") and row.startswith(glob[:-2])) for glob in globs)
    }


def evidence(prefix: str, license_files: list[str]) -> dict[str, object]:
    names = {"license", "license.md", "license.txt", "copying", "copyright"}
    selected = sorted(path for path in license_files if path.startswith(prefix) and Path(path).name.lower() in names)
    digest = hashlib.sha256()
    for row in selected:
        digest.update(row.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(SOURCE / row)))
        digest.update(b"\n")
    return {"path_prefix": prefix, "count": len(selected), "paths_sha256": digest.hexdigest()}


def component(component_id: str, paths: set[str], *, globs: list[str], copy_allowed: bool, license: str, disposition: str, license_evidence: list[dict[str, object]], dependency_manifests: list[dict[str, str]] | None = None) -> dict[str, object]:
    return {
        "component_id": component_id,
        "selection": "glob",
        "path_globs": globs,
        "exclude_globs": [],
        "file_count": len(paths),
        "bytes": sum((SOURCE / row).stat().st_size for row in paths),
        "tree_sha256": tree_digest(paths),
        "license": license,
        "spdx_expression": "MIT" if copy_allowed else "NOASSERTION",
        "copy_allowed": copy_allowed,
        "runtime_import_allowed": False,
        "disposition": disposition,
        "license_evidence": license_evidence,
        "notice_evidence": [],
        "dependency_manifests": dependency_manifests or [],
    }


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    regular, symlinks = tracked_paths()
    license_files = list(manifest["license_files"])
    dependency_paths = list(manifest["dependency_manifests"])
    direct_license = {"LICENSE"}
    direct_readme = {"README.md"}
    direct_src = select(regular, ["src/**"])
    quarantine_internal = select(regular, ["internal/cbm/**", "vendored/**"])
    excluded_runtime = select(regular, ["graph-ui/**", "tools/**", "pkg/**"])
    covered = direct_license | direct_readme | direct_src | quarantine_internal | excluded_runtime
    residual = regular - covered
    dep_entries = [{"path": row, "sha256": sha256_file(SOURCE / row)} for row in dependency_paths]
    components = [
        component("cbm-root-license", direct_license, globs=["LICENSE"], copy_allowed=True, license="MIT", disposition="direct-transfer-eligible", license_evidence=[{"path": "LICENSE", "sha256": sha256_file(SOURCE / "LICENSE")}]),
        component("cbm-root-readme", direct_readme, globs=["README.md"], copy_allowed=True, license="MIT", disposition="direct-transfer-eligible", license_evidence=[{"path": "LICENSE", "sha256": sha256_file(SOURCE / "LICENSE")}]),
        component("cbm-owner-authored-src", direct_src, globs=["src/**"], copy_allowed=True, license="MIT", disposition="direct-transfer-eligible-with-review", license_evidence=[evidence("src/", license_files)]),
        component("cbm-internal-and-vendored", quarantine_internal, globs=["internal/cbm/**", "vendored/**"], copy_allowed=False, license="mixed-or-component-review-required", disposition="quarantine-clean-room-only", license_evidence=[evidence("internal/cbm/", license_files), evidence("vendored/", license_files)], dependency_manifests=dep_entries),
        component("cbm-generated-runtime-and-data", excluded_runtime, globs=["graph-ui/**", "tools/**", "pkg/**"], copy_allowed=False, license="not-adopted", disposition="excluded", license_evidence=[]),
        {
            **component("cbm-residual-complement", residual, globs=[], copy_allowed=False, license="NOASSERTION", disposition="quarantine-complement", license_evidence=[]),
            "selection": "git-tracked-regular-file-complement",
        },
    ]
    data = {
        "schema_version": "bhm.p28.wi68.component-license-sbom-inventory.v1",
        "task_id": "BHM-P28-CBM-99-CAPABILITY-TRANSFER-20260722",
        "source_id": manifest["source_id"],
        "revision": manifest["upstream_commit_or_tag"],
        "source_content_sha256": manifest["content_sha256"],
        "source_manifest": {"path": ".src/codebase-memory-mcp/SOURCE-MANIFEST.json", "sha256": sha256_file(MANIFEST)},
        "policy": {
            "direct_transfer_scope": manifest["covered_files"],
            "direct_transfer_components": ["cbm-root-license", "cbm-root-readme", "cbm-owner-authored-src"],
            "excluded_classes": ["vendored", "generated", "runtime", "data", "unverified-complement"],
        },
        "totals": {"regular_files": len(regular), "symlinks": len(symlinks), "git_entries": len(regular) + len(symlinks), "license_file_count_observed": len(license_files), "dependency_manifest_count_observed": len(dependency_paths)},
        "components": components,
        "review": {"reviewer": "Codex /root", "reviewed_at": "2026-07-23", "recheck_date": "2026-10-21", "status": "partial-evidence; no unverified component enters runtime"},
    }
    replace_bytes_safely(OUT, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    print(json.dumps({"path": str(OUT), "regular_files": len(regular), "symlinks": len(symlinks), "components": len(components)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
