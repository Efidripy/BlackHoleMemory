"""Build a bounded WI-17 product-value and pruning report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.product_value import build_product_value_benchmark


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
    report = build_product_value_benchmark(**payload)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        _write_report(args.report, rendered)
    return 0 if all(bool(value) for value in report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
