"""Preview the dry-run WI-14 migration/compatibility plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.migration_compatibility import build_migration_preview


def _write_report(path: Path | None, rendered: str) -> None:
    if path is not None:
        replace_bytes_safely(path.expanduser(), (rendered + "\n").encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload: dict[str, Any] = {}
    if args.fixture:
        payload = json.loads(args.fixture.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("fixture root must be an object")
    records = payload.pop("records", [])
    preview = build_migration_preview(records, **payload)
    rendered = json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        _write_report(args.report, rendered)
    return 0 if all(preview["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
