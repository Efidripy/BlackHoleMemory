#!/usr/bin/env python3
"""Acquire or verify operator-reviewed sources under the ignored .src zone."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.source_registry import SourceRegistryError, load_registry, sync_source, verify_registry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=Path("config/source-registry.json"))
    parser.add_argument("--source-root", type=Path, default=Path(".src"))
    parser.add_argument("--only", action="append", default=[], help="source id or slug; repeatable")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def _write_report(path: Path, result: dict[str, object]) -> None:
    replace_bytes_safely(
        path,
        (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def main() -> int:
    args = parse_args()
    try:
        registry = load_registry(args.registry)
        selected = set(args.only)
        events: list[dict[str, object]] = []
        if not args.verify_only:
            for source in registry["sources"]:
                if selected and source["id"] not in selected and source["slug"] not in selected:
                    continue
                try:
                    manifest = sync_source(source, args.source_root, refresh=args.refresh)
                    events.append(
                        {
                            "source_id": source["id"],
                            "slug": source["slug"],
                            "status": manifest["acquisition_status"],
                            "revision": manifest["upstream_commit_or_tag"],
                            "content_sha256": manifest["content_sha256"],
                        }
                    )
                except SourceRegistryError as exc:
                    events.append({"source_id": source["id"], "slug": source["slug"], "status": "error", "error": str(exc)})
        validation = verify_registry(args.registry, args.source_root)
        result = {
            "schema_version": "bhm.source-quarantine-sync.v1",
            "ok": validation["ok"] and not any(event.get("status") == "error" for event in events),
            "events": events,
            "validation": validation,
            "source_root": str(args.source_root.resolve()),
            "writes_live_state": False,
        }
        if args.report:
            _write_report(args.report, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    except (OSError, ValueError, SourceRegistryError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "writes_live_state": False}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
