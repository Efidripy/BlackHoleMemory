"""Bounded, metadata-only package and module manifest resolution.

This is a clean-room CBM parity surface.  It reads a small allowlist of
manifest files from an operator-approved repository root and returns package
identities only.  Versions, URLs, credentials and manifest contents never
cross the API boundary and nothing is persisted.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .filesystem_boundaries import assert_safe_path
from .package_resolution_base import (
    CONSTRAINT_PROVENANCE_SCHEMA_VERSION,
    DEPENDENCY_PROVENANCE_SCHEMA_VERSION,
    DependencyProvenanceError,
    MANIFEST_IDENTITY_SCHEMA_VERSION,
    MAX_JSON_DEPTH,  # noqa: F401
    MAX_JSON_NODES,  # noqa: F401
    MAX_LOCKFILE_BYTES,  # noqa: F401
    MAX_LOCKFILE_DEPENDENCIES,  # noqa: F401
    MAX_LOCKFILES,  # noqa: F401
    MAX_MANIFEST_BYTES,
    MAX_MANIFESTS,
    MAX_PACKAGES,
    MAX_TRAVERSAL_ENTRIES,  # noqa: F401
    PACKAGE_RESOLUTION_SCHEMA_VERSION,
    PackageResolutionError,
    _BLOCKED_PARTS,
    _LOCKFILE_NAMES,  # noqa: F401
    _MANIFEST_NAMES,
    _NAME_RE,  # noqa: F401
    _REQ_RE,  # noqa: F401
    _bounded_files,
    _constraint_descriptor,  # noqa: F401
    _constraint_variant,
    _digest,
    _manifest_identity,
    _package_identity,
    _safe_name,  # noqa: F401
)
from .package_resolution_lockfiles import (
    _lock_row,  # noqa: F401
    _lockfile_identity,  # noqa: F401
    _parse_lock_lines,  # noqa: F401
    _parse_lockfile,  # noqa: F401
    _parse_nuget_lock,  # noqa: F401
    _parse_package_lock,  # noqa: F401
    _parse_package_resolved,  # noqa: F401
    resolve_dependency_provenance,
)
from .package_resolution_manifests import (
    _add,  # noqa: F401
    _parse_gomod,  # noqa: F401
    _parse_gradle,  # noqa: F401
    _parse_json,  # noqa: F401
    _parse_lines,  # noqa: F401
    _parse_manifest,
    _parse_maven,  # noqa: F401
    _parse_nuget_project,  # noqa: F401
    _parse_pubspec,  # noqa: F401
    _parse_toml,  # noqa: F401
    _safe_gomod_module,  # noqa: F401
    _valid_gomod_selector,  # noqa: F401
)


def resolve_package_manifests(root: str | Path, *, limit: int = 64) -> dict[str, Any]:
    """Return deterministic package identities from bounded manifests."""

    if not 1 <= int(limit) <= MAX_MANIFESTS:
        raise PackageResolutionError(f"limit must be between 1 and {MAX_MANIFESTS}")
    # lgtm [py/path-injection]
    base = assert_safe_path(
        Path(root).expanduser(),
        reject_hardlink_target=False,
    )
    if not base.is_dir():
        raise PackageResolutionError("repository root is not a directory")
    manifests: list[tuple[Path, str]] = []
    for path in _bounded_files(base):
        if not path.is_file() or any(part.casefold() in _BLOCKED_PARTS for part in path.relative_to(base).parts):
            continue
        name = path.name.casefold()
        ecosystem = _MANIFEST_NAMES.get(name)
        if ecosystem is None and path.suffix.casefold() == ".csproj":
            ecosystem = "nuget"
        if ecosystem is None and name.startswith("requirements-") and name.endswith(".txt"):
            ecosystem = "python"
        if ecosystem is None and path.suffix.casefold() == ".gemspec":
            ecosystem = "ruby"
        if ecosystem:
            manifests.append((path, ecosystem))
        if len(manifests) >= int(limit):
            break
    rows: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []
    for path, ecosystem in manifests:
        relative = path.relative_to(base).as_posix()
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        if len(payload) > MAX_MANIFEST_BYTES:
            continue
        content_digest = _digest(payload)
        manifest_id = _manifest_identity(
            relative_path=relative,
            ecosystem=ecosystem,
            content_digest=content_digest,
        )
        parsed = _parse_manifest(payload, relative, ecosystem)
        manifest_rows.append(
            {
                "path": relative,
                "ecosystem": ecosystem,
                "sha256": content_digest,
                "manifest_id": manifest_id,
                "identity_schema_version": MANIFEST_IDENTITY_SCHEMA_VERSION,
                "package_count": len(parsed),
            }
        )
        # Bind each parsed package to deterministic manifest evidence without
        # carrying manifest contents or absolute host paths.
        for row in parsed:
            row["manifest_id"] = manifest_id
        rows.extend(parsed)
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["ecosystem"]), str(row["name"]).casefold(), str(row["dependency_kind"]))
        existing = unique.get(key)
        if existing is None:
            first = dict(row)
            first["constraint_variants"] = [_constraint_variant(row)]
            unique[key] = first
            continue
        variants = existing.setdefault("constraint_variants", [])
        variant = _constraint_variant(row)
        if variant not in variants:
            variants.append(variant)
            variants[:] = sorted(variants, key=lambda item: (item["dependency_kind"], item["constraint_kind"], item["constraint_digest"]))[:32]
        identities = set(existing.get("manifest_ids") or [])
        if existing.get("manifest_id"):
            identities.add(str(existing["manifest_id"]))
        if row.get("manifest_id"):
            identities.add(str(row["manifest_id"]))
        existing["manifest_ids"] = sorted(identities)
        existing.pop("manifest_id", None)
    for row in unique.values():
        if "manifest_ids" not in row and row.get("manifest_id"):
            row["manifest_ids"] = [str(row.pop("manifest_id"))]
        row.setdefault("constraint_variants", [_constraint_variant(row)])
        row["package_id"] = _package_identity(row)
    packages = sorted(unique.values(), key=lambda item: (item["ecosystem"], item["name"].casefold(), item["dependency_kind"], item["manifest"]))[:MAX_PACKAGES]
    edge_keys: set[tuple[str, str, str, str]] = set()
    dependency_edges: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row["manifest"]), str(row["ecosystem"]), str(row["name"]), str(row["dependency_kind"]))
        if key in edge_keys:
            continue
        edge_keys.add(key)
        dependency_edges.append(
            {
                "source_manifest": row["manifest"],
                "source_manifest_id": str(row.get("manifest_id") or ""),
                "target_package": row["name"],
                "ecosystem": row["ecosystem"],
                "dependency_kind": row["dependency_kind"],
                "edge_kind": "declares_dependency",
                "authoritative": False,
                "promotion_eligible": False,
            }
        )
    dependency_edges.sort(key=lambda item: (item["ecosystem"], item["source_manifest"], item["target_package"].casefold(), item["dependency_kind"]))
    dependency_edges = dependency_edges[:MAX_PACKAGES]
    manifest_set_digest = _digest(json.dumps([
        {
            "path": str(item.get("path") or ""),
            "ecosystem": str(item.get("ecosystem") or ""),
            "manifest_id": str(item.get("manifest_id") or ""),
            "sha256": str(item.get("sha256") or ""),
        }
        for item in sorted(manifest_rows, key=lambda value: (str(value.get("ecosystem") or ""), str(value.get("path") or "")))
    ], sort_keys=True, separators=(",", ":")).encode("utf-8"))
    core = {
        "manifest_count": len(manifest_rows),
        "package_count": len(packages),
        "constraint_provenance_schema_version": CONSTRAINT_PROVENANCE_SCHEMA_VERSION,
        "constraint_kind_counts": dict(sorted(Counter(str(row.get("constraint_kind") or "unspecified") for row in packages).items())),
        "manifests": manifest_rows,
        "packages": packages,
        "dependency_edges": dependency_edges,
        "manifest_set_digest": manifest_set_digest,
        "bounded": True,
        "raw_source_returned": False,
    }
    digest = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema_version": PACKAGE_RESOLUTION_SCHEMA_VERSION,
        "manifest_identity_schema_version": MANIFEST_IDENTITY_SCHEMA_VERSION,
        "resolution_digest": digest,
        **core,
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "raw_source_returned": False, "model_started": False},
    }


__all__ = [
    "DEPENDENCY_PROVENANCE_SCHEMA_VERSION",
    "CONSTRAINT_PROVENANCE_SCHEMA_VERSION",
    "MANIFEST_IDENTITY_SCHEMA_VERSION",
    "PACKAGE_RESOLUTION_SCHEMA_VERSION",
    "DependencyProvenanceError",
    "PackageResolutionError",
    "resolve_dependency_provenance",
    "resolve_package_manifests",
]
