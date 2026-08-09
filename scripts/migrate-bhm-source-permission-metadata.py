#!/usr/bin/env python3
"""Migrate quarantined BHM source metadata to the permission-aware v2 shape.

The migration is intentionally textual and bounded: it touches only the
tracked source registry and the 33 v1 source manifests below ``.src``.  The
separately maintained ``python-docker-image`` provenance manifest is not a
source-registry entry and is left unchanged.  ``--check`` is the default;
``--apply`` performs the reversible metadata-only rewrite.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from blackholememory.filesystem_boundaries import replace_bytes_safely


REGISTRY_SCHEMA_V1 = "bhm.source-registry.v1"
REGISTRY_SCHEMA_V2 = "bhm.source-registry.v2"
MANIFEST_SCHEMA_V1 = "bhm.source-manifest.v1"
MANIFEST_SCHEMA_V2 = "bhm.source-manifest.v2"
PERMISSION_BLOCK = (
    '"permission_status": "not-mapped",\n'
    '"permission_evidence_ref": null,\n'
    '"rightsholder": null,\n'
    '"covered_scope": null,\n'
    '"covered_files": [],\n'
    '"covered_capabilities": [],\n'
    '"third_party_exclusions": [],\n'
    '"permission_checked_at": null,\n'
)


def _read(path: Path) -> tuple[str, str]:
    raw = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def _write(path: Path, text: str, newline: str) -> None:
    replace_bytes_safely(path, text.replace("\n", newline).encode("utf-8"))


def _insert_permission_block(text: str, marker: str, indent: str) -> str:
    """Insert defaults after legacy markers, but never duplicate v2 fields."""
    lines = text.splitlines(keepends=True)
    rendered: list[str] = []
    block = "".join(f"{indent}{line}" for line in PERMISSION_BLOCK.splitlines(True))
    for index, line in enumerate(lines):
        rendered.append(line)
        if line != marker:
            continue
        lookahead = "".join(lines[index + 1 : index + 1 + len(PERMISSION_BLOCK.splitlines())])
        if '"permission_status"' not in lookahead:
            rendered.append(block)
    return "".join(rendered)


def _migrate_registry(path: Path, apply: bool) -> dict[str, int]:
    text, newline = _read(path)
    original = text
    text = text.replace(f'"schema_version": "{REGISTRY_SCHEMA_V1}"', f'"schema_version": "{REGISTRY_SCHEMA_V2}"', 1)
    policy_marker = '    "reviewer": "Codex /root"\n  },'
    policy_replacement = (
        '    "reviewer": "Codex /root",\n'
        '    "permission_model": "bhm.permission-metadata.v1",\n'
        '    "permission_default_status": "not-mapped",\n'
        '    "permission_private_correspondence": "forbidden"\n'
        '  },'
    )
    text = text.replace(policy_marker, policy_replacement, 1)
    source_marker = '      "code_copy_allowed": false,\n'
    source_count = text.count(source_marker)
    text = _insert_permission_block(text, source_marker, "      ")
    changed = int(text != original)
    if apply and changed:
        _write(path, text, newline)
    return {"source_entries": source_count, "changed": changed}


def _migrate_manifests(root: Path, apply: bool) -> dict[str, int]:
    manifests = sorted(root.glob("*/SOURCE-MANIFEST.json"))
    eligible = 0
    total = 0
    changed = 0
    for path in manifests:
        text, newline = _read(path)
        if f'"schema_version": "{MANIFEST_SCHEMA_V1}"' not in text and f'"schema_version": "{MANIFEST_SCHEMA_V2}"' not in text:
            continue
        total += 1
        if f'"schema_version": "{MANIFEST_SCHEMA_V1}"' not in text:
            continue
        eligible += 1
        original = text
        text = text.replace(f'"schema_version": "{MANIFEST_SCHEMA_V1}"', f'"schema_version": "{MANIFEST_SCHEMA_V2}"', 1)
        marker = '  "code_copy_allowed": false,\n'
        if marker not in text:
            raise RuntimeError(f"{path}: v1 manifest lacks code_copy_allowed marker")
        text = _insert_permission_block(text, marker, "  ")
        if text != original:
            changed += 1
            if apply:
                _write(path, text, newline)
    return {"manifest_entries": total, "v1_entries": eligible, "changed": changed}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true", help="write the v2 metadata migration")
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    registry = _migrate_registry(repo / "config" / "source-registry.json", args.apply)
    manifests = _migrate_manifests(repo / ".src", args.apply)
    if registry["source_entries"] != 33:
        raise RuntimeError(f"expected 33 registry sources, found {registry['source_entries']}")
    if manifests["manifest_entries"] != 33:
        raise RuntimeError(f"expected 33 source manifests, found {manifests['manifest_entries']}")
    mode = "applied" if args.apply else "check-only"
    print(f"mode={mode} registry={registry} manifests={manifests} extra_non_registry_manifests=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
