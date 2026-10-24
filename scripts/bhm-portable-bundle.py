#!/usr/bin/env python
"""Build or dry-run-validate a bounded redacted BHM portable bundle.

The input is a disposable JSON snapshot projection.  No live database,
Qdrant collection or Mem0 state is read or changed by this command.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.filesystem_boundaries import read_bytes_safely, replace_bytes_safely
from blackholememory.portable_bundle import MAX_BUNDLE_BYTES, build_portable_bundle, dry_run_import


REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = REPO_ROOT / ".runtime" / "portable-bundles"


def _runtime_path(path: Path) -> Path:
    root = BUNDLE_ROOT.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path must remain below {root}") from exc
    if resolved.suffix.casefold() != ".json":
        raise ValueError("portable bundle paths must use .json")
    return resolved


def _load_json(path: Path) -> dict:
    raw = read_bytes_safely(path, max_bytes=MAX_BUNDLE_BYTES)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON input must be an object")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="build a redacted bundle from a disposable snapshot")
    export.add_argument("--input", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--project", required=True)
    export.add_argument("--producer-revision", required=True)
    export.add_argument("--source-snapshot-digest", required=True)
    export.add_argument("--created-at", required=True)
    preview = sub.add_parser("preview", help="validate a bundle without applying it")
    preview.add_argument("--input", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "export":
        bundle = build_portable_bundle(
            _load_json(args.input),
            project=args.project,
            producer_revision=args.producer_revision,
            source_snapshot_digest=args.source_snapshot_digest,
            created_at=args.created_at,
        )
        output = _runtime_path(args.output)
        rendered = (json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(rendered) > MAX_BUNDLE_BYTES:
            raise ValueError("portable bundle exceeds bounded size")
        replace_bytes_safely(output, rendered)
        print(json.dumps({"output": str(output), "bundle_digest": bundle["bundle_digest"]}, sort_keys=True))
        return 0
    receipt = dry_run_import(_load_json(args.input))
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
