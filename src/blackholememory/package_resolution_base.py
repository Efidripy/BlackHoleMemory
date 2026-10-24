"""Bounded package resolution schemas, constants, exceptions, and base helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

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
    "packages.lock.json": "nuget",
    "project.assets.json": "nuget",
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


def _constraint_variant(row: Mapping[str, Any]) -> dict[str, str]:
    """Return the redacted constraint identity used for conflict receipts."""

    return {
        "dependency_kind": str(row.get("dependency_kind") or "unspecified")[:32],
        "constraint_kind": str(row.get("constraint_kind") or "unspecified")[:32],
        "constraint_digest": str(row.get("constraint_digest") or "")[:64],
    }


__all__ = [
    "CONSTRAINT_PROVENANCE_SCHEMA_VERSION",
    "DEPENDENCY_PROVENANCE_SCHEMA_VERSION",
    "DependencyProvenanceError",
    "MANIFEST_IDENTITY_SCHEMA_VERSION",
    "MAX_JSON_DEPTH",
    "MAX_JSON_NODES",
    "MAX_LOCKFILE_BYTES",
    "MAX_LOCKFILE_DEPENDENCIES",
    "MAX_LOCKFILES",
    "MAX_MANIFEST_BYTES",
    "MAX_MANIFESTS",
    "MAX_PACKAGES",
    "MAX_TRAVERSAL_ENTRIES",
    "PACKAGE_RESOLUTION_SCHEMA_VERSION",
    "PackageResolutionError",
    "_BLOCKED_PARTS",
    "_LOCKFILE_NAMES",
    "_MANIFEST_NAMES",
    "_NAME_RE",
    "_REQ_RE",
    "_bounded_files",
    "_constraint_descriptor",
    "_constraint_variant",
    "_digest",
    "_manifest_identity",
    "_package_identity",
    "_safe_name",
]
