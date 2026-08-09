"""Bounded, metadata-only package and module manifest resolution.

This is a clean-room CBM parity surface.  It reads a small allowlist of
manifest files from an operator-approved repository root and returns package
identities only.  Versions, URLs, credentials and manifest contents never
cross the API boundary and nothing is persisted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path

PACKAGE_RESOLUTION_SCHEMA_VERSION = "bhm.package-resolution.v1"
MANIFEST_IDENTITY_SCHEMA_VERSION = "bhm.package-manifest-identity.v1"
CONSTRAINT_PROVENANCE_SCHEMA_VERSION = "bhm.dependency-constraint-provenance.v1"
DEPENDENCY_PROVENANCE_SCHEMA_VERSION = "bhm.dependency-provenance.v1"
MAX_MANIFESTS = 64
MAX_PACKAGES = 256
MAX_MANIFEST_BYTES = 512 * 1024
MAX_LOCKFILES = 64
MAX_LOCKFILE_DEPENDENCIES = 512
MAX_LOCKFILE_BYTES = 2 * 1024 * 1024
MAX_TRAVERSAL_ENTRIES = 8_192
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 4_096
_BLOCKED_PARTS = {".git", ".src", ".venv", "venv", "node_modules", "dist", "build", "runtime", "__pycache__", ".pytest_cache"}
_MANIFEST_NAMES = {
    "package.json": "npm",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "go.mod": "go",
    "cargo.toml": "rust",
    "composer.json": "php",
    "pubspec.yaml": "dart",
    "pom.xml": "java",
    "build.gradle": "jvm",
    "build.gradle.kts": "jvm",
    "mix.exs": "elixir",
    "gemfile": "ruby",
    "pipfile": "python",
    "package.swift": "swift",
    "deno.json": "javascript",
    "deno.jsonc": "javascript",
}
_LOCKFILE_NAMES = {
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
    "pnpm-lock.yml": "pnpm",
    "poetry.lock": "poetry",
    "cargo.lock": "rust",
    "go.sum": "go",
    "gemfile.lock": "ruby",
    "package.resolved": "swift",
}
_NAME_RE = re.compile(r"^[A-Za-z0-9_@./:+-]{1,180}$")
_REQ_RE = re.compile(r"^[A-Za-z0-9_.@/-]+")


class PackageResolutionError(ValueError):
    """Raised when a package-resolution request violates a safe bound."""


class DependencyProvenanceError(ValueError):
    """Raised when a lockfile provenance request violates a safe bound."""


def _bounded_files(base: Path, *, max_entries: int = MAX_TRAVERSAL_ENTRIES):
    """Yield repository files without materializing an unbounded tree."""

    remaining = max(1, int(max_entries))
    for raw_dir, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
        safe_dirs = sorted(
            name for name in dirnames if name.casefold() not in _BLOCKED_PARTS
        )
        if len(safe_dirs) >= remaining:
            dirnames[:] = safe_dirs[:remaining]
            remaining = 0
        else:
            dirnames[:] = safe_dirs
            remaining -= len(safe_dirs)
        if remaining <= 0:
            return
        for name in sorted(filenames):
            if remaining <= 0:
                return
            remaining -= 1
            yield Path(raw_dir) / name


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _manifest_identity(*, relative_path: str, ecosystem: str, content_digest: str) -> str:
    """Return a root-neutral identity for one observed manifest.

    The identity deliberately binds only a repository-relative path, the
    normalized ecosystem and the content digest.  Absolute host paths never
    enter the public package-resolution digest or response, which keeps the
    evidence portable between checkouts while still detecting content drift.
    """

    payload = "\x00".join((relative_path, ecosystem, content_digest)).encode("utf-8")
    return _digest(payload)


def _package_identity(row: Mapping[str, Any]) -> str:
    """Return a root-neutral package identity without exposing selectors."""

    payload = {
        "ecosystem": str(row.get("ecosystem") or "").casefold()[:32],
        "qualified_name": str(row.get("qualified_name") or row.get("name") or "")[:200],
        "manifest_ids": sorted(str(item)[:64] for item in (row.get("manifest_ids") or []) if str(item).strip()),
    }
    return _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _safe_name(value: Any) -> str:
    text = str(value or "").strip().strip('"\'')
    if not text or text.startswith("-") or not _NAME_RE.fullmatch(text):
        return ""
    return text[:180]


def _constraint_descriptor(value: Any, ecosystem: str) -> tuple[str, str] | None:
    """Classify one literal dependency selector without exposing its value."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        text = "mapping"
    else:
        text = str(value or "").strip().strip('"\'')
    if not text:
        return ("unspecified", "")
    lowered = text.casefold()
    if lowered.startswith(("workspace:", "workspace*", "workspace^", "workspac~", "link:")):
        kind = "workspace"
    elif lowered in {"*", "latest", "any"} or ("*" in text and not lowered.startswith(("git", "http", "npm:", "jsr:"))):
        kind = "wildcard"
    elif lowered.startswith(("file:", "./", "../", "/", "~")):
        kind = "local"
    elif lowered.startswith(("git+", "git:", "git@", "ssh:", "http:", "https:", "jsr:", "npm:")) or "://" in lowered:
        kind = "remote"
    elif re.search(r"(?:^|[<>=~^]|\s)[0-9]+(?:\.[0-9xX*]+){0,3}(?:[-+][A-Za-z0-9_.-]+)?(?:\s|$)", text) or any(token in text for token in ("<", ">", "=", "~", "^")):
        kind = "range" if any(token in text for token in ("<", ">", "~", "^", "*", "x", "X")) else "exact"
    elif re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){0,3}(?:[-+][A-Za-z0-9_.-]+)?", text):
        kind = "exact"
    elif text == "mapping":
        kind = "opaque"
    else:
        kind = "opaque"
    digest = _digest(f"{ecosystem}\x00{kind}\x00{text}".encode("utf-8")) if text else ""
    return kind, digest


def _add(
    rows: list[dict[str, Any]],
    *,
    name: Any,
    ecosystem: str,
    manifest: str,
    kind: str,
    qualified_name: Any = "",
    constraint: Any = None,
) -> None:
    safe = _safe_name(name)
    if not safe:
        return
    row = {"name": safe, "ecosystem": ecosystem, "manifest": manifest, "dependency_kind": kind}
    qualified = _safe_name(qualified_name)
    if qualified:
        row["qualified_name"] = qualified
    descriptor = _constraint_descriptor(constraint, ecosystem)
    if descriptor is not None:
        row["constraint_kind"], row["constraint_digest"] = descriptor
    rows.append(row)


def _constraint_variant(row: Mapping[str, Any]) -> dict[str, str]:
    """Return the redacted constraint identity used for conflict receipts."""

    return {
        "dependency_kind": str(row.get("dependency_kind") or "unspecified")[:32],
        "constraint_kind": str(row.get("constraint_kind") or "unspecified")[:32],
        "constraint_digest": str(row.get("constraint_digest") or "")[:64],
    }


def _parse_json(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for key, kind in (("dependencies", "runtime"), ("devDependencies", "development"), ("peerDependencies", "peer"), ("optionalDependencies", "optional"), ("require", "runtime"), ("require-dev", "development")):
        value = data.get(key)
        if isinstance(value, Mapping):
            for name, constraint in value.items():
                _add(rows, name=name, ecosystem=ecosystem, manifest=manifest, kind=kind, constraint=constraint)
        elif isinstance(value, list):
            for name in value:
                _add(rows, name=name, ecosystem=ecosystem, manifest=manifest, kind=kind)
    return rows


def _parse_toml(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    try:
        data = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    rows: list[dict[str, Any]] = []
    sections: list[tuple[Mapping[str, Any], str]] = []
    project = data.get("project") if isinstance(data, Mapping) else None
    if isinstance(project, Mapping):
        dependencies = project.get("dependencies")
        if isinstance(dependencies, list):
            for value in dependencies:
                match = _REQ_RE.match(str(value))
                _add(rows, name=match.group(0) if match else "", ecosystem=ecosystem, manifest=manifest, kind="runtime", constraint=value)
        optional = project.get("optional-dependencies")
        if isinstance(optional, Mapping):
            for values in optional.values():
                if isinstance(values, list):
                    for value in values:
                        _add(rows, name=_REQ_RE.match(str(value)).group(0) if _REQ_RE.match(str(value)) else "", ecosystem=ecosystem, manifest=manifest, kind="optional", constraint=value)
    for key, kind in (("dependencies", "runtime"), ("dev-dependencies", "development"), ("build-dependencies", "build")):
        value = data.get(key)
        if isinstance(value, Mapping):
            sections.append((value, kind))
    tool = data.get("tool") if isinstance(data, Mapping) else None
    if isinstance(tool, Mapping):
        poetry = tool.get("poetry")
        if isinstance(poetry, Mapping):
            for key, kind in (("dependencies", "runtime"), ("dev-dependencies", "development")):
                value = poetry.get(key)
                if isinstance(value, Mapping):
                    sections.append((value, kind))
    for section, kind in sections:
        for name, constraint in section.items():
            if name not in {"python", "python_requires"}:
                _add(rows, name=name, ecosystem=ecosystem, manifest=manifest, kind=kind, constraint=constraint)
    return rows


def _parse_lines(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    rows: list[dict[str, Any]] = []
    manifest_name = Path(manifest).name.casefold()
    if manifest_name == "requirements.txt" or manifest_name.startswith("requirements-") and manifest_name.endswith(".txt"):
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            match = _REQ_RE.match(line)
            _add(rows, name=match.group(0) if match else "", ecosystem=ecosystem, manifest=manifest, kind="runtime", constraint=line)
        return rows
    if manifest_name == "gemfile":
        for line in text.splitlines():
            match = re.match(r"^\s*gem\s+['\"]([^'\"]{1,180})['\"]", line)
            if match:
                _add(rows, name=match.group(1), ecosystem=ecosystem, manifest=manifest, kind="runtime", constraint=None)
        return rows
    if manifest_name == "pipfile":
        section = ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1].casefold()
                continue
            if section in {"packages", "dev-packages"}:
                match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=", line)
                if match:
                    value = line.split("=", 1)[1].strip() if "=" in line else None
                    _add(rows, name=match.group(1), ecosystem=ecosystem, manifest=manifest, kind="development" if section == "dev-packages" else "runtime", constraint=value)
        return rows
    if manifest_name == "package.swift":
        for line in text.splitlines():
            match = re.search(r"\.package\s*\(\s*(?:url:\s*)?[\"']([^\"']+)[\"']", line)
            if match:
                identity = match.group(1).rstrip("/").rsplit("/", 1)[-1]
                if identity.endswith(".git"):
                    identity = identity[:-4]
                _add(rows, name=identity, ecosystem=ecosystem, manifest=manifest, kind="runtime")
        return rows
    if manifest_name in {"deno.json", "deno.jsonc"}:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}
        imports = data.get("imports") if isinstance(data, Mapping) else None
        if isinstance(imports, Mapping):
            for name, target in imports.items():
                value = str(target or "")
                if value.startswith(("npm:", "jsr:")):
                    _add(rows, name=str(name), ecosystem=ecosystem, manifest=manifest, kind="runtime", constraint=value)
        return rows
    for line in text.splitlines():
        match = re.search(r"(?:add_dependency|implementation|api|compile|runtimeOnly|devImplementation)\s*\(?\s*['\"]([^'\"]+)", line)
        if match:
            kind = "development" if "dev" in line.casefold() else "runtime"
            _add(rows, name=match.group(1), ecosystem=ecosystem, manifest=manifest, kind=kind)
    return rows


def _safe_gomod_module(value: Any) -> str:
    """Return a public Go module identity, rejecting paths/selectors."""

    text = str(value or "").strip().strip('"\'')
    if (
        not text
        or text.startswith((".", "/", "~"))
        or "\\" in text
        or "://" in text
        or text.startswith(("git@", "file:"))
        or any(char.isspace() for char in text)
        or re.fullmatch(r"v?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9_.-]+)?", text)
    ):
        return ""
    return _safe_name(text)


def _valid_gomod_selector(value: Any) -> bool:
    """Recognize a bounded Go version/range before hashing it."""

    text = str(value or "").strip()
    if not text:
        return False
    if re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?", text):
        return True
    return bool(re.fullmatch(r"\[\s*v\d+\.\d+\.\d+\s*,\s*v\d+\.\d+\.\d+\s*\]", text))


def _parse_gomod(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    """Extract redacted Go module directive identities without evaluation."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    rows: list[dict[str, Any]] = []
    block: str | None = None
    directives = {"module", "go", "toolchain", "require", "replace", "exclude", "retract"}

    for raw in text.splitlines():
        # Go mod comments are line comments.  Removing them before parsing
        # prevents commented-out or inline look-alike directives from leaking
        # into the metadata surface.
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line == ")":
            block = None
            continue
        if block:
            directive, payload_text = block, line
        else:
            match = re.match(r"^(module|go|toolchain|require|replace|exclude|retract)\b(.*)$", line)
            if not match:
                continue
            directive = match.group(1)
            payload_text = match.group(2).strip()
            if payload_text == "(":
                block = directive
                continue
            if payload_text.startswith("("):
                block = directive
                payload_text = payload_text[1:].strip()
                if not payload_text:
                    continue
        if directive not in directives or not payload_text:
            continue

        if directive == "module":
            name = _safe_gomod_module(payload_text.split()[0])
            if name:
                _add(rows, name=name, ecosystem=ecosystem, manifest=manifest, kind="module")
            continue
        if directive == "go":
            version = payload_text.split()[0]
            _add(rows, name="go", ecosystem=ecosystem, manifest=manifest, kind="language", constraint=version)
            continue
        if directive == "toolchain":
            version = payload_text.split()[0]
            _add(rows, name="toolchain", ecosystem=ecosystem, manifest=manifest, kind="toolchain", constraint=version)
            continue
        if directive == "replace":
            sides = re.split(r"\s+=>\s+", payload_text, maxsplit=1)
            if len(sides) != 2:
                continue
            left = sides[0].split()
            right = sides[1].split()
            old = _safe_gomod_module(left[0] if left else "")
            if not old:
                continue
            target = right[0] if right else ""
            # The source module identity is useful; the replacement operand
            # is represented only by a redacted constraint digest.  A safe
            # module target may be included as a qualified identity, while
            # local paths and URLs are deliberately discarded.
            target_identity = _safe_gomod_module(target)
            row = {"name": old, "ecosystem": ecosystem, "manifest": manifest, "dependency_kind": "replace"}
            descriptor = _constraint_descriptor(" ".join(right), ecosystem)
            if descriptor is not None:
                row["constraint_kind"], row["constraint_digest"] = descriptor
            if target_identity:
                row["qualified_name"] = target_identity
            rows.append(row)
            continue
        parts = payload_text.split()
        name = _safe_gomod_module(parts[0] if parts else "")
        if not name:
            continue
        kind = {"require": "runtime", "exclude": "exclude", "retract": "retract"}[directive]
        selector = " ".join(parts[1:])
        if not _valid_gomod_selector(selector):
            continue
        _add(rows, name=name, ecosystem=ecosystem, manifest=manifest, kind=kind, constraint=selector)
    return rows


def _parse_maven(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    """Extract bounded Maven dependency coordinates from dependency blocks."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    rows: list[dict[str, Any]] = []
    for block in re.findall(r"<dependency\b[^>]*>(?P<body>.*?)</dependency\s*>", text, re.IGNORECASE | re.DOTALL):
        group = re.search(r"<groupId>\s*([^<\s]{1,180})\s*</groupId>", block, re.IGNORECASE)
        artifact = re.search(r"<artifactId>\s*([^<\s]{1,180})\s*</artifactId>", block, re.IGNORECASE)
        if not group or not artifact:
            continue
        qualified = f"{group.group(1)}:{artifact.group(1)}"
        version = re.search(r"<version>\s*([^<\s]{1,180})\s*</version>", block, re.IGNORECASE)
        _add(rows, name=artifact.group(1), qualified_name=qualified, ecosystem=ecosystem, manifest=manifest, kind="runtime", constraint=version.group(1) if version else None)
    return rows


def _parse_gradle(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    """Extract literal Gradle dependency coordinates without evaluating DSL."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    rows: list[dict[str, Any]] = []
    pattern = re.compile(r"\b(?P<configuration>implementation|api|compileOnly|runtimeOnly|testImplementation|developmentOnly)\s*(?:\(\s*)?[\"'](?P<coordinate>[^\"']{1,240})[\"']", re.IGNORECASE)
    for match in pattern.finditer(text):
        coordinate = str(match.group("coordinate") or "").strip()
        parts = coordinate.split(":")
        name = parts[-2] if len(parts) >= 2 else coordinate
        kind = "development" if str(match.group("configuration") or "").casefold().startswith("test") else "runtime"
        _add(rows, name=name, qualified_name=coordinate if len(parts) >= 2 else "", ecosystem=ecosystem, manifest=manifest, kind=kind, constraint=parts[-1] if len(parts) >= 3 else None)
    return rows


def _parse_pubspec(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    """Extract first-level Dart dependency identities without a YAML runtime."""

    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    rows: list[dict[str, Any]] = []
    section = ""
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if not raw.startswith((" ", "\t")) and stripped.endswith(":"):
            key = stripped[:-1].strip().casefold()
            section = {"dependencies": "runtime", "dev_dependencies": "development"}.get(key, "")
            continue
        if not section:
            continue
        match = re.match(r"^\s{2,}([A-Za-z0-9_.-]+)\s*:", raw)
        if match:
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else None
            _add(rows, name=match.group(1), ecosystem=ecosystem, manifest=manifest, kind=section, constraint=value)
    return rows


def _parse_manifest(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    if Path(manifest).name.casefold() == "go.mod":
        return _parse_gomod(payload, manifest, ecosystem)
    if Path(manifest).name.casefold() in {"deno.json", "deno.jsonc"}:
        return _parse_lines(payload, manifest, ecosystem)
    if manifest.casefold().endswith(".json"):
        return _parse_json(payload, manifest, ecosystem)
    if manifest.casefold().endswith(".toml"):
        return _parse_toml(payload, manifest, ecosystem)
    if Path(manifest).name.casefold() == "pom.xml":
        return _parse_maven(payload, manifest, ecosystem)
    if Path(manifest).name.casefold() in {"build.gradle", "build.gradle.kts"}:
        return _parse_gradle(payload, manifest, ecosystem)
    if manifest.casefold().endswith("pubspec.yaml"):
        return _parse_pubspec(payload, manifest, ecosystem)
    return _parse_lines(payload, manifest, ecosystem)


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
    elif ecosystem in {"rust", "ruby", "python", "swift"} and " " in text:
        text = text.split(None, 1)[0]
    text = text.rstrip(":").strip()
    return _safe_name(text)


def _lock_row(name: Any, *, ecosystem: str, manifest: str, unresolved: bool = False) -> dict[str, Any] | None:
    identity = _lockfile_identity(name, ecosystem)
    if not identity:
        return None
    return {
        "name": identity,
        "ecosystem": ecosystem,
        "manifest": manifest,
        "dependency_kind": "transitive",
        "transitive": True,
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
