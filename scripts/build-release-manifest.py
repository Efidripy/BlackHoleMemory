"""Create a deterministic content manifest for a staged BHM release."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


TRUST_METADATA_FILES = {
    "release-manifest.json",
    "sbom.spdx.json",
    "provenance.json",
    "release-trust.json",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: Path, expected_version: str) -> dict[str, object]:
    root = root.resolve()
    expected = str(expected_version).lstrip("v")
    version_path = root / "config" / "version-manifest.json"
    if not version_path.exists():
        raise SystemExit(f"missing version manifest: {version_path}")
    payload = json.loads(version_path.read_text(encoding="utf-8"))
    if str(payload.get("release_version") or "") != expected:
        raise SystemExit("staged version manifest does not match expected release")

    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in TRUST_METADATA_FILES:
            continue
        relative = path.relative_to(root).as_posix()
        if "__pycache__/" in relative or relative.endswith(".pyc"):
            continue
        files.append({"path": relative, "size": path.stat().st_size, "sha256": sha256(path)})

    return {
        "schema_version": 1,
        "product": "BlackHoleMemory",
        "release_version": expected,
        "file_count": len(files),
        "files": files,
    }


def write_manifest(root: Path, manifest: dict[str, object]) -> None:
    (root / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="staged release root")
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    manifest = build_manifest(root, args.expected_version)
    write_manifest(root, manifest)
    print(json.dumps({"ok": True, "release_version": manifest["release_version"], "file_count": manifest["file_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
