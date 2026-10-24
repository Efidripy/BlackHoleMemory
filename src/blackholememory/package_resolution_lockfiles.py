"""Bounded inventory and parsing of repository lockfiles."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path
from .package_resolution_base import (
    _BLOCKED_PARTS,
    _LOCKFILE_NAMES,
    DEPENDENCY_PROVENANCE_SCHEMA_VERSION,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_LOCKFILE_BYTES,
    MAX_LOCKFILE_DEPENDENCIES,
    MAX_LOCKFILES,
    DependencyProvenanceError,
    _bounded_files,
    _digest,
    _safe_name,
)


def _lockfile_identity(value: Any, ecosystem: str) -> str:
    """Derive an identity without carrying selectors, versions, URLs or paths."""

    text = str(value or "").strip().strip("\"'")
    if not text or text.startswith((".", "/", "#")):
        return ""
    # Lockfiles may contain source URLs and VCS selectors beside identities;
    # those are never an admissible public dependency identity.
    if "://" in text or text.startswith(("git+", "http:", "https:")):
        return ""
    # package-lock/pnpm nested package keys and Cargo/Poetry names.
    if "/node_modules/" in text:
        text = text.rsplit("/node_modules/", 1)[-1]
    if text.startswith("node_modules/"):
        text = text[len("node_modules/") :]
    if text.startswith("/"):
        text = text[1:]
    if ecosystem in {"npm", "yarn", "pnpm"}:
        # Selectors are intentionally discarded.  Scoped names contain one @
        # at the beginning, so split only at the last selector separator.
        if text.startswith("@"):
            separator = text.find("@", 1)
            if separator > 0:
                text = text[:separator]
        elif "@" in text:
            text = text.split("@", 1)[0]
    elif ecosystem == "nuget" and "/" in text:
        package, selector = text.rsplit("/", 1)
        if package and re.fullmatch(r"[0-9][A-Za-z0-9_.+-]{0,119}", selector):
            text = package
    elif ecosystem in {"rust", "ruby", "python", "swift"} and " " in text:
        text = text.split(None, 1)[0]
    text = text.rstrip(":").strip()
    return _safe_name(text)


def _lock_row(
    name: Any,
    *,
    ecosystem: str,
    manifest: str,
    unresolved: bool = False,
    transitive: bool = True,
) -> dict[str, Any] | None:
    identity = _lockfile_identity(name, ecosystem)
    if not identity:
        return None
    return {
        "name": identity,
        "ecosystem": ecosystem,
        "manifest": manifest,
        "dependency_kind": "transitive" if transitive else "direct",
        "transitive": bool(transitive),
        "resolution_status": "unresolved" if unresolved else "resolved",
    }


def _parse_package_lock(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return []
    candidates: list[Any] = []
    if isinstance(data, Mapping):
        packages = data.get("packages")
        if isinstance(packages, Mapping):
            candidates.extend(str(key) for key in packages if str(key))
        dependencies = data.get("dependencies")
        if isinstance(dependencies, Mapping):
            stack: list[tuple[Mapping[str, Any], int]] = [(dependencies, 0)]
            nodes = 0
            while stack and nodes < MAX_JSON_NODES:
                value, depth = stack.pop()
                if depth > MAX_JSON_DEPTH:
                    continue
                for key, child in value.items():
                    nodes += 1
                    if nodes > MAX_JSON_NODES:
                        break
                    candidates.append(str(key))
                    nested = child.get("dependencies") if isinstance(child, Mapping) else None
                    if isinstance(nested, Mapping) and depth < MAX_JSON_DEPTH:
                        stack.append((nested, depth + 1))
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        row = _lock_row(candidate, ecosystem=ecosystem, manifest=manifest)
        if row:
            rows.append(row)
    return rows


def _parse_package_resolved(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return []
    rows: list[dict[str, Any]] = []
    stack: list[tuple[Any, int]] = [(data, 0)]
    nodes = 0
    while stack and nodes < MAX_JSON_NODES:
        value, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            continue
        nodes += 1
        if isinstance(value, Mapping):
            if "identity" in value:
                row = _lock_row(value.get("identity"), ecosystem=ecosystem, manifest=manifest)
                if row:
                    rows.append(row)
            elif "package" in value and isinstance(value.get("package"), str):
                row = _lock_row(value.get("package"), ecosystem=ecosystem, manifest=manifest)
                if row:
                    rows.append(row)
            if depth < MAX_JSON_DEPTH:
                stack.extend((child, depth + 1) for child in value.values() if isinstance(child, (Mapping, list)))
        elif isinstance(value, list) and depth < MAX_JSON_DEPTH:
            stack.extend((child, depth + 1) for child in value if isinstance(child, (Mapping, list)))
    return rows


def _parse_nuget_lock(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    """Extract NuGet identities from packages.lock.json or project.assets.json."""

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return []
    if not isinstance(data, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    name = Path(manifest).name.casefold()
    if name == "packages.lock.json":
        frameworks = data.get("dependencies")
        if isinstance(frameworks, Mapping):
            for dependencies in frameworks.values():
                if not isinstance(dependencies, Mapping):
                    continue
                for package_name, metadata in dependencies.items():
                    is_direct = isinstance(metadata, Mapping) and str(metadata.get("type") or "").casefold() == "direct"
                    row = _lock_row(
                        package_name,
                        ecosystem=ecosystem,
                        manifest=manifest,
                        transitive=not is_direct,
                    )
                    if row:
                        rows.append(row)
        return rows
    libraries = data.get("libraries")
    if isinstance(libraries, Mapping):
        for package_key, metadata in libraries.items():
            if isinstance(metadata, Mapping) and str(metadata.get("type") or "package").casefold() != "package":
                continue
            row = _lock_row(package_key, ecosystem=ecosystem, manifest=manifest)
            if row:
                rows.append(row)
    return rows


def _parse_lock_lines(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    name = Path(manifest).name.casefold()
    rows: list[dict[str, Any]] = []
    if name == "go.sum":
        for line in text.splitlines():
            parts = line.strip().split()
            if parts:
                row = _lock_row(parts[0], ecosystem=ecosystem, manifest=manifest)
                if row:
                    rows.append(row)
        return rows
    if name == "gemfile.lock":
        in_specs = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "specs:":
                in_specs = True
                continue
            if in_specs and stripped and not line.startswith(" "):
                in_specs = False
            if in_specs and line.startswith("    ") and not line.startswith("      "):
                row = _lock_row(stripped.split("(", 1)[0].strip(), ecosystem=ecosystem, manifest=manifest)
                if row:
                    rows.append(row)
        return rows
    if name == "poetry.lock":
        for line in text.splitlines():
            match = re.match(r"^name\s*=\s*[\"']([^\"']+)", line.strip())
            if match:
                row = _lock_row(match.group(1), ecosystem=ecosystem, manifest=manifest)
                if row:
                    rows.append(row)
        return rows
    if name == "cargo.lock":
        for line in text.splitlines():
            match = re.match(r"^name\s*=\s*[\"']([^\"']+)", line.strip())
            if match:
                row = _lock_row(match.group(1), ecosystem=ecosystem, manifest=manifest)
                if row:
                    rows.append(row)
        return rows
    if name == "yarn.lock":
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "__metadata:")) or line.startswith((" ", "\t")) or not stripped.endswith(":"):
                continue
            for selector in stripped[:-1].split(","):
                row = _lock_row(selector, ecosystem=ecosystem, manifest=manifest)
                if row:
                    rows.append(row)
        return rows
    if name in {"pnpm-lock.yaml", "pnpm-lock.yml"}:
        # pnpm keys are `/name@selector:` or `'name@selector':`; ignore
        # importer metadata and package URLs by requiring a package-like key.
        for line in text.splitlines():
            stripped = line.strip().strip("'\"")
            if not stripped or stripped.startswith(("lockfileVersion:", "importers:", "packages:", "snapshots:")):
                continue
            if stripped.startswith("/"):
                candidate = stripped[1:].split(":", 1)[0]
            elif stripped.endswith(":") and "@" in stripped:
                candidate = stripped[:-1]
            else:
                continue
            row = _lock_row(candidate, ecosystem=ecosystem, manifest=manifest)
            if row:
                rows.append(row)
        return rows
    return rows


def _parse_lockfile(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    name = Path(manifest).name.casefold()
    if name in {"package-lock.json", "npm-shrinkwrap.json"}:
        return _parse_package_lock(payload, manifest, ecosystem)
    if name == "package.resolved":
        return _parse_package_resolved(payload, manifest, ecosystem)
    if name in {"packages.lock.json", "project.assets.json"}:
        return _parse_nuget_lock(payload, manifest, ecosystem)
    return _parse_lock_lines(payload, manifest, ecosystem)


def resolve_dependency_provenance(root: str | Path, *, limit: int = MAX_LOCKFILES) -> dict[str, Any]:
    """Inventory local lockfiles without resolving, installing, or exposing source.

    The result is intentionally evidence-only: package identities and aggregate
    digests are returned, while versions, URLs, credentials and lockfile text
    never leave this boundary.  No package manager, network call or persistence
    is performed.
    """

    if not 1 <= int(limit) <= MAX_LOCKFILES:
        raise DependencyProvenanceError(f"limit must be between 1 and {MAX_LOCKFILES}")
    # lgtm [py/path-injection]
    base = assert_safe_path(
        Path(root).expanduser(),
        reject_hardlink_target=False,
    )
    if not base.is_dir():
        raise DependencyProvenanceError("repository root is not a directory")
    lockfiles: list[tuple[Path, str]] = []
    for path in _bounded_files(base):
        if not path.is_file() or any(part.casefold() in _BLOCKED_PARTS for part in path.relative_to(base).parts):
            continue
        ecosystem = _LOCKFILE_NAMES.get(path.name.casefold())
        if ecosystem:
            lockfiles.append((path, ecosystem))
        if len(lockfiles) >= int(limit):
            break
    rows: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    for path, ecosystem in lockfiles:
        relative = path.relative_to(base).as_posix()
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        digest = _digest(payload)
        if len(payload) > MAX_LOCKFILE_BYTES:
            manifests.append({"path": relative, "lockfile_kind": path.name.casefold(), "ecosystem": ecosystem, "sha256": digest, "dependency_count": 0, "unresolved_count": 0, "transitive_count": 0, "status": "unresolved", "bounded_skip": "size_limit"})
            continue
        parsed = _parse_lockfile(payload, relative, ecosystem)
        rows.extend(parsed)
        manifests.append({"path": relative, "lockfile_kind": path.name.casefold(), "ecosystem": ecosystem, "sha256": digest, "dependency_count": len(parsed), "unresolved_count": sum(row["resolution_status"] == "unresolved" for row in parsed), "transitive_count": sum(bool(row["transitive"]) for row in parsed), "status": "resolved" if parsed else "unresolved", "bounded_skip": None})
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["manifest"]), str(row["ecosystem"]), str(row["name"]).casefold())
        unique.setdefault(key, row)
    dependencies = sorted(unique.values(), key=lambda item: (item["ecosystem"], item["manifest"], item["name"].casefold()))[:MAX_LOCKFILE_DEPENDENCIES]
    summary = {
        "lockfile_count": len(manifests),
        "dependency_count": len(dependencies),
        "transitive_count": sum(bool(row["transitive"]) for row in dependencies),
        "unresolved_count": sum(row["resolution_status"] == "unresolved" for row in dependencies) + sum(item["unresolved_count"] for item in manifests if item["status"] == "unresolved" and not item["dependency_count"]),
        "status": "resolved" if manifests and all(item["status"] == "resolved" for item in manifests) else ("unresolved" if manifests else "no_lockfiles"),
    }
    core = {
        "bounded": True,
        "lockfiles": manifests,
        "dependencies": dependencies,
        "summary": summary,
        "raw_lockfile_returned": False,
        "network_used": False,
        "package_manager_used": False,
    }
    digest = _digest(json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {
        "schema_version": DEPENDENCY_PROVENANCE_SCHEMA_VERSION,
        "provenance_digest": digest,
        **core,
        "provenance": {"source": "local-lockfile", "authority": "observed-metadata", "transitive_status_explicit": True, "versions_exposed": False, "urls_exposed": False, "credentials_exposed": False},
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "raw_source_returned": False, "network_used": False, "package_manager_used": False, "runtime_import": False, "model_started": False},
    }


__all__ = [
    "_lock_row",
    "_lockfile_identity",
    "_parse_lock_lines",
    "_parse_lockfile",
    "_parse_nuget_lock",
    "_parse_package_lock",
    "_parse_package_resolved",
    "resolve_dependency_provenance",
]
