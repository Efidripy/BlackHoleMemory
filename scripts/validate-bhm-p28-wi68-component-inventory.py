"""Verify the bounded P28/WI-68 CBM component evidence inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_files(source_root: Path) -> tuple[set[str], set[str]]:
    rows = subprocess.check_output(
        ["git", "-C", str(source_root), "ls-files"], text=True
    ).splitlines()
    regular: set[str] = set()
    symlinks: set[str] = set()
    for row in rows:
        if row.startswith(".git/"):
            continue
        path = source_root / row
        if path.is_symlink():
            symlinks.add(row)
        elif path.is_file():
            regular.add(row)
    return regular, symlinks


def tree_digest(source_root: Path, paths: set[str]) -> str:
    digest = hashlib.sha256()
    for row in sorted(paths):
        path = source_root / row
        digest.update(row.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def matches(row: str, glob: str) -> bool:
    if glob.endswith("/**"):
        return row.startswith(glob[:-2])
    return row == glob


def component_paths(component: dict, regular: set[str], covered: set[str]) -> set[str]:
    if component.get("selection") == "git-tracked-regular-file-complement":
        return regular - covered
    selected = {
        row
        for row in regular
        if any(matches(row, glob) for glob in component.get("path_globs", []))
    }
    for glob in component.get("exclude_globs", []):
        selected = {row for row in selected if not matches(row, glob)}
    return selected


def verify_evidence(source_root: Path, entries: list[dict], failures: list[str], label: str) -> None:
    for item in entries:
        if "path" in item:
            path = source_root / item["path"]
            if not path.is_file():
                failures.append(f"{label}: missing {item['path']}")
            elif sha256(path) != item["sha256"]:
                failures.append(f"{label}: hash drift {item['path']}")
            continue
        prefix = item.get("path_prefix")
        if not prefix:
            failures.append(f"{label}: malformed evidence entry")
            continue
        paths = [
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.relative_to(source_root).as_posix().startswith(prefix)
            and path.name.lower() in {"license", "license.md", "license.txt", "copying", "copyright"}
        ]
        digest = hashlib.sha256()
        for path in sorted(paths, key=lambda value: value.relative_to(source_root).as_posix()):
            row = path.relative_to(source_root).as_posix()
            digest.update(row.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
            digest.update(b"\n")
        if len(paths) != item.get("count"):
            failures.append(f"{label}: evidence count {prefix}")
        if digest.hexdigest() != item.get("paths_sha256"):
            failures.append(f"{label}: evidence digest {prefix}")


def verify_dependency_manifests(
    source_root: Path, entries: list[dict], failures: list[str], label: str
) -> None:
    for item in entries:
        path = source_root / item["path"]
        if not path.is_file():
            failures.append(f"{label}: missing dependency manifest {item['path']}")
        elif sha256(path) != item["sha256"]:
            failures.append(f"{label}: dependency manifest hash drift {item['path']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inventory",
        default=".docs/ops/bhm-p28-wi68-component-license-sbom-inventory-2026-07-23.json",
    )
    parser.add_argument("--report")
    args = parser.parse_args()

    inventory_path = ROOT / args.inventory
    source_root = ROOT / ".src" / "codebase-memory-mcp" / "source"
    manifest_path = ROOT / ".src" / "codebase-memory-mcp" / "SOURCE-MANIFEST.json"
    data = json.loads(inventory_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    regular, symlinks = git_files(source_root)
    failures: list[str] = []

    if sha256(manifest_path) != data["source_manifest"]["sha256"]:
        failures.append("SOURCE-MANIFEST.json hash drift")
    for key in ("source_id", "revision", "source_content_sha256"):
        manifest_key = {"source_id": "source_id", "revision": "upstream_commit_or_tag", "source_content_sha256": "content_sha256"}[key]
        if data[key] != manifest.get(manifest_key):
            failures.append(f"manifest mismatch: {key}")
    if len(manifest.get("license_files", [])) != data["totals"]["license_file_count_observed"]:
        failures.append("license file total")
    if len(manifest.get("dependency_manifests", [])) != data["totals"]["dependency_manifest_count_observed"]:
        failures.append("dependency manifest total")
    if manifest.get("covered_files") != data["policy"]["direct_transfer_scope"]:
        failures.append("direct transfer scope drift")

    covered: set[str] = set()
    direct_names = set(data["policy"]["direct_transfer_components"])
    components = data["components"]
    for component in components:
        paths = component_paths(component, regular, covered)
        if component.get("selection") != "git-tracked-regular-file-complement":
            overlap = covered & paths
            if overlap:
                failures.append(f"component overlap: {component['component_id']}")
        covered |= paths
        if len(paths) != component["file_count"]:
            failures.append(f"{component['component_id']}: file_count")
        if sum((source_root / row).stat().st_size for row in paths) != component["bytes"]:
            failures.append(f"{component['component_id']}: bytes")
        if tree_digest(source_root, paths) != component["tree_sha256"]:
            failures.append(f"{component['component_id']}: tree_sha256")
        if component.get("copy_allowed") and component["component_id"] not in direct_names:
            failures.append(f"unauthorized direct component: {component['component_id']}")
        if component.get("runtime_import_allowed"):
            failures.append(f"runtime import enabled: {component['component_id']}")
        verify_evidence(source_root, component.get("license_evidence", []), failures, component["component_id"])
        verify_evidence(source_root, component.get("notice_evidence", []), failures, component["component_id"])
        verify_dependency_manifests(
            source_root,
            component.get("dependency_manifests", []),
            failures,
            component["component_id"],
        )

    if covered != regular:
        failures.append(f"coverage mismatch: {len(regular - covered)} regular files unaccounted")
    if len(symlinks) != data["totals"]["symlinks"]:
        failures.append("symlink count")
    if len(regular) != data["totals"]["regular_files"]:
        failures.append("regular file total")
    if len(regular) + len(symlinks) != data["totals"]["git_entries"]:
        failures.append("git entry total")

    report = {
        "schema_version": "bhm.p28.wi68.component-license-sbom-inventory-check.v1",
        "inventory": args.inventory,
        "source_id": data["source_id"],
        "regular_files": len(regular),
        "symlinks": len(symlinks),
        "covered_files": len(covered),
        "writes_live_state": False,
        "failures": failures,
        "ok": not failures,
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        Path(args.report).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
