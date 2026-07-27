"""Capture deterministic evidence for the environment consumed by a release build."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import platform
import sys
import tomllib
from pathlib import Path


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized(value: str) -> str:
    return "-".join(str(value).strip().lower().replace("_", "-").replace(".", "-").split("-"))


def installed_file_digest(distribution: metadata.Distribution) -> str:
    rows: list[dict[str, str]] = []
    for item in distribution.files or ():
        path = Path(distribution.locate_file(item))
        if path.is_file():
            rows.append({"path": str(item).replace("\\", "/"), "sha256": digest_file(path)})
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def capture(root: Path, output: Path, *, source_revision: str, launcher: Path | None, uv_version: str) -> dict[str, object]:
    lock_path = root / "uv.lock"
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages: list[dict[str, object]] = []
    installed = {normalized(dist.metadata["Name"]): dist for dist in metadata.distributions() if dist.metadata.get("Name")}
    for row in lock.get("package", []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        version = str(row.get("version") or "")
        if not name or not version or normalized(name) == normalized("BlackHoleMemory"):
            continue
        dist = installed.get(normalized(name))
        if dist is None or str(dist.version) != version:
            # The lock can contain optional/test groups not selected by the
            # release profile; only consumed installed distributions belong in
            # the receipt and SBOM.
            continue
        packages.append(
            {
                "name": name,
                "version": version,
                "evidence_class": "installed_file_set",
                "installed_file_set_sha256": installed_file_digest(dist),
            }
        )
    receipt = {
        "schema_version": 1,
        "evidence_class": "installed_file_set",
        "source_revision": source_revision,
        "lock_sha256": digest_file(lock_path),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "interpreter": sys.executable,
        "pyinstaller_version": metadata.version("pyinstaller"),
        "uv_version": uv_version,
        "launcher_sha256": digest_file(launcher) if launcher and launcher.is_file() else "",
        "packages": sorted(packages, key=lambda item: (str(item["name"]), str(item["version"]))),
    }
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--launcher", type=Path)
    parser.add_argument("--uv-version", default="unknown")
    args = parser.parse_args()
    result = capture(
        args.root.resolve(),
        args.output.resolve(),
        source_revision=args.source_revision,
        launcher=args.launcher.resolve() if args.launcher else None,
        uv_version=args.uv_version,
    )
    print(json.dumps({"ok": True, "package_count": len(result["packages"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
