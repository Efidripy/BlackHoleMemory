"""Verify a staged or archived BHM release without executing the binary."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath


REQUIRED_FILES = {
    "BHM_Launcher.exe",
    "config/version-manifest.json",
    "pyproject.toml",
    "uv.lock",
    "plugins/bhm-codex-connector/.codex-plugin/plugin.json",
    "scripts/bhm_launcher.py",
    "src/blackholememory/app.py",
    "src/blackholememory/version_manifest.py",
    "release-manifest.json",
}
LICENSE_REQUIRED_FROM = (1, 8, 0)
CURRENT_P9_REQUIRED_FILES = {
    "scripts/start-bhm-authoritative.ps1",
    "scripts/validate-bhm-streamable-http.ps1",
    "scripts/bhm-projection-operator.ps1",
}
TRUST_METADATA_FILES = {
    "release-manifest.json",
    "sbom.spdx.json",
    "provenance.json",
    "release-trust.json",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version_tuple(value: str) -> tuple[int, int, int]:
    parts = str(value).lstrip("v").split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"unsupported semantic version: {value}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and "\\" not in name
        and not path.is_absolute()
        and ".." not in path.parts
        and "" not in path.parts
    )


def verify_mapping(files: dict[str, bytes], expected_version: str) -> dict[str, object]:
    failures: list[str] = []
    manifest_bytes = files.get("release-manifest.json")
    if manifest_bytes is None:
        failures.append("missing release-manifest.json")
        return {"ok": False, "failures": failures}
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"ok": False, "failures": [f"invalid release manifest: {exc}"]}

    expected = str(expected_version).lstrip("v")
    try:
        parsed_version = version_tuple(expected)
    except ValueError as exc:
        return {"ok": False, "failures": [str(exc)]}
    if manifest.get("schema_version") != 1:
        failures.append("unsupported release manifest schema")
    if manifest.get("product") != "BlackHoleMemory":
        failures.append("release manifest product mismatch")
    if str(manifest.get("release_version") or "") != expected:
        failures.append("release version mismatch")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        failures.append("release manifest files must be an array")
        entries = []
    listed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            failures.append("release manifest contains a non-object entry")
            continue
        relative = str(entry.get("path") or "")
        if not safe_member(relative):
            failures.append(f"unsafe listed file: {relative}")
            continue
        if relative in listed:
            failures.append(f"duplicate listed file: {relative}")
            continue
        listed.add(relative)
        payload = files.get(relative)
        if payload is None:
            failures.append(f"missing listed file: {relative}")
            continue
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size != len(payload):
            failures.append(f"size mismatch: {relative}")
        if str(entry.get("sha256") or "") != digest(payload):
            failures.append(f"hash mismatch: {relative}")
    actual = set(files) - TRUST_METADATA_FILES
    if actual - listed:
        failures.extend(f"unlisted file: {name}" for name in sorted(actual - listed))
    if len(entries) != int(manifest.get("file_count") or -1):
        failures.append("release manifest file_count mismatch")
    for required in REQUIRED_FILES - {"release-manifest.json"}:
        if required not in files:
            failures.append(f"missing required file: {required}")
    if parsed_version >= LICENSE_REQUIRED_FROM and "LICENSE" not in files:
        failures.append("missing required file: LICENSE")
    # v1.7.0 is a sealed historical baseline.  Current releases must carry
    # the P9 operational scripts so an archive cannot pass while omitting the
    # lifecycle/recovery validators introduced after that baseline.
    if expected != "1.7.0":
        for required in CURRENT_P9_REQUIRED_FILES:
            if required not in files:
                failures.append(f"missing required file: {required}")

    try:
        version = json.loads(files["config/version-manifest.json"].decode("utf-8"))
        if str(version.get("release_version") or "") != expected:
            failures.append("embedded version manifest mismatch")
        plugin = json.loads(files["plugins/bhm-codex-connector/.codex-plugin/plugin.json"].decode("utf-8"))
        if str(plugin.get("version") or "") != expected:
            failures.append("embedded plugin version mismatch")
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        failures.append(f"embedded metadata invalid: {exc}")

    return {
        "ok": not failures,
        "release_version": expected,
        "file_count": len(files),
        "manifest_file_count": len(entries),
        "failures": failures,
    }


def verify_directory(root: Path) -> dict[str, object]:
    bundle = root / "BlackHoleMemory" if (root / "BlackHoleMemory").is_dir() else root
    files = {
        path.relative_to(bundle).as_posix(): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc")
    }
    return files


def verify_archive(path: Path) -> tuple[dict[str, bytes], list[str]]:
    failures: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            failures.append("archive contains duplicate members")
        for name in names:
            if not safe_member(name):
                failures.append(f"unsafe archive member: {name}")
        manifest_names = [
            name for name in names if name == "release-manifest.json" or name.endswith("/release-manifest.json")
        ]
        if len(manifest_names) != 1:
            failures.append("archive must contain exactly one release-manifest.json")
        manifest_name = manifest_names[0] if len(manifest_names) == 1 else ""
        prefix = manifest_name[: -len("release-manifest.json")] if manifest_name else ""
        files = {
            name[len(prefix) :]: archive.read(name)
            for name in names
            if name.startswith(prefix) and not name.endswith("/")
        }
        if prefix:
            failures.extend(
                f"archive member outside release root: {name}"
                for name in names
                if not name.endswith("/") and not name.startswith(prefix)
            )
    return files, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--release-root", type=Path)
    source.add_argument("--archive", type=Path)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()

    archive_failures: list[str] = []
    if args.archive:
        files, archive_failures = verify_archive(args.archive.resolve())
    else:
        files = verify_directory(args.release_root.resolve())
    result = verify_mapping(files, args.expected_version)
    result["failures"] = archive_failures + list(result.get("failures") or [])
    result["ok"] = not result["failures"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
