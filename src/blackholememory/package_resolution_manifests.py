"""Parsing multi-ecosystem repository manifest files."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, Mapping

from .package_resolution_base import (
    _REQ_RE,
    _constraint_descriptor,
    _safe_name,
)


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


def _parse_nuget_project(payload: bytes, manifest: str, ecosystem: str) -> list[dict[str, Any]]:
    """Extract literal ``PackageReference`` identities without MSBuild evaluation."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    rows: list[dict[str, Any]] = []
    pattern = re.compile(
        r"<PackageReference\b(?P<attrs>[^>]{0,2000}?)(?:/\s*>|>(?P<body>.*?)</PackageReference\s*>)",
        re.IGNORECASE | re.DOTALL,
    )
    attribute_pattern = re.compile(
        r"\b(?P<name>Include|Update|Version|PrivateAssets)\s*=\s*[\"'](?P<value>[^\"']{0,500})[\"']",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        attrs = {
            str(item.group("name") or "").casefold(): str(item.group("value") or "").strip()
            for item in attribute_pattern.finditer(str(match.group("attrs") or ""))
        }
        body = str(match.group("body") or "")
        package_name = attrs.get("include") or attrs.get("update") or ""
        if not package_name or "$" in package_name:
            continue
        version = attrs.get("version")
        if version is None:
            version_match = re.search(r"<Version>\s*([^<]{1,500})\s*</Version>", body, re.IGNORECASE)
            version = str(version_match.group(1)).strip() if version_match else None
        private_assets = attrs.get("privateassets")
        if private_assets is None:
            private_match = re.search(r"<PrivateAssets>\s*([^<]{1,120})\s*</PrivateAssets>", body, re.IGNORECASE)
            private_assets = str(private_match.group(1)).strip() if private_match else ""
        kind = "development" if str(private_assets or "").casefold() == "all" else "runtime"
        _add(rows, name=package_name, ecosystem=ecosystem, manifest=manifest, kind=kind, constraint=version)
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
    if Path(manifest).suffix.casefold() == ".csproj":
        return _parse_nuget_project(payload, manifest, ecosystem)
    if Path(manifest).name.casefold() in {"build.gradle", "build.gradle.kts"}:
        return _parse_gradle(payload, manifest, ecosystem)
    if manifest.casefold().endswith("pubspec.yaml"):
        return _parse_pubspec(payload, manifest, ecosystem)
    return _parse_lines(payload, manifest, ecosystem)


__all__ = [
    "_add",
    "_parse_gomod",
    "_parse_gradle",
    "_parse_json",
    "_parse_lines",
    "_parse_manifest",
    "_parse_maven",
    "_parse_nuget_project",
    "_parse_pubspec",
    "_parse_toml",
    "_safe_gomod_module",
    "_valid_gomod_selector",
]
