"""Deterministic provenance and publication-boundary checks for P28.

The boundary is deliberately read-only: it validates source manifests and
their content digests, records a stable aggregate digest, and rejects
quarantine material in Git or release/package artifacts.  It never imports or
executes files from ``.src`` and never mutates runtime state.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .source_registry import verify_registry
from .resource_limits import PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS


PROVENANCE_BOUNDARY_SCHEMA = "bhm.p28.provenance-boundary.v1"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_paths(root: Path, *args: str) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        return ["git-check-unavailable"]
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def canonical_provenance_digest(source_results: Iterable[dict[str, Any]]) -> str:
    """Return a stable digest over manifest identity, not source payload."""

    rows = []
    for item in source_results:
        rows.append(
            {
                "source_id": str(item.get("source_id") or ""),
                "slug": str(item.get("slug") or ""),
                "revision": str(item.get("revision") or ""),
                "content_sha256": str(item.get("content_sha256") or ""),
                "manifest_sha256": str(item.get("manifest_sha256") or ""),
                "acquisition_status": str(item.get("acquisition_status") or ""),
                "disposition": str(item.get("disposition") or ""),
                "code_copy_allowed": bool(item.get("code_copy_allowed")),
                "permission_status": str(item.get("permission_status") or ""),
            }
        )
    canonical = json.dumps(sorted(rows, key=lambda row: (row["source_id"], row["slug"])), separators=(",", ":"), sort_keys=True)
    return _sha256_bytes(canonical.encode("utf-8"))


def scan_package_boundary(path: Path) -> dict[str, Any]:
    """Check one release directory or ZIP for quarantined ``.src`` entries."""

    resolved = path.resolve()
    residues: list[str] = []
    if resolved.is_dir():
        for child in resolved.rglob("*"):
            relative = child.relative_to(resolved).as_posix()
            if ".src" in PurePosixPath(relative).parts:
                residues.append(relative)
    elif resolved.is_file() and resolved.suffix.casefold() == ".zip":
        try:
            with zipfile.ZipFile(resolved) as archive:
                for name in archive.namelist():
                    if ".src" in PurePosixPath(name.replace("\\", "/")).parts:
                        residues.append(name)
        except (OSError, zipfile.BadZipFile) as exc:
            return {"path": str(resolved), "checked": False, "residue": [], "error": str(exc), "ok": False}
    else:
        return {"path": str(resolved), "checked": False, "residue": [], "error": "path is not a directory or zip", "ok": False}
    return {"path": str(resolved), "checked": True, "residue": sorted(set(residues)), "ok": not residues}


def build_provenance_boundary_report(
    repo_root: Path,
    *,
    package_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    """Build a read-only provenance report suitable for an evidence receipt."""

    root = repo_root.resolve()
    registry_path = root / "config" / "source-registry.json"
    source_root = root / ".src"
    registry_report = verify_registry(registry_path, source_root)
    failures = list(registry_report.get("failures") or [])

    tracked = _git_paths(root, "ls-files", "--", ".src")
    staged = _git_paths(root, "diff", "--cached", "--name-only", "--", ".src")
    visible_untracked = _git_paths(root, "ls-files", "--others", "--exclude-standard", "--", ".src")
    if tracked != [] and tracked != ["git-check-unavailable"]:
        failures.append(".src tracked paths present")
    if staged != [] and staged != ["git-check-unavailable"]:
        failures.append(".src staged paths present")
    if visible_untracked != [] and visible_untracked != ["git-check-unavailable"]:
        failures.append("visible non-ignored .src paths present")
    if "git-check-unavailable" in tracked + staged + visible_untracked:
        failures.append("git source boundary unavailable")

    gitignore = (root / ".gitignore").read_text(encoding="utf-8") if (root / ".gitignore").is_file() else ""
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8") if (root / ".dockerignore").is_file() else ""
    ignore_boundary = {"git": ".src/" in gitignore, "docker": ".src/" in dockerignore}
    if not all(ignore_boundary.values()):
        failures.append(".src ignore boundary missing")

    package_reports = [scan_package_boundary(path) for path in package_paths]
    failures.extend(
        f"package boundary failed: {item['path']}"
        for item in package_reports
        if not item.get("ok")
    )
    manifest_rows = registry_report.get("source_results") or []
    return {
        "schema_version": PROVENANCE_BOUNDARY_SCHEMA,
        "ok": not failures,
        "registry": {
            "ok": bool(registry_report.get("ok")),
            "source_count": registry_report.get("source_count", 0),
            "registry_sha256": registry_report.get("registry_sha256"),
            "manifest_count": len(manifest_rows),
            "provenance_digest": canonical_provenance_digest(manifest_rows),
        },
        "source_boundary": {
            "tracked": [] if tracked == ["git-check-unavailable"] else tracked,
            "staged": [] if staged == ["git-check-unavailable"] else staged,
            "visible_untracked": [] if visible_untracked == ["git-check-unavailable"] else visible_untracked,
            "ignore": ignore_boundary,
            "clean": not any(value and value != ["git-check-unavailable"] for value in (tracked, staged, visible_untracked)),
        },
        "package_boundary": {"checked": bool(package_reports), "artifacts": package_reports, "clean": all(item.get("ok") for item in package_reports) if package_reports else True},
        "execution": {"writes_sqlite": False, "writes_qdrant": False, "writes_runtime": False, "imports_quarantine": False},
        "failures": failures,
    }
