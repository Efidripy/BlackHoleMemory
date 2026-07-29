"""Safely activate the bounded parser v2 graph for one repository."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.parser_activation import ParserActivationError
from blackholememory.parser_activation import activate_parser_v2
from blackholememory.parser_activation import write_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "runtime" / "live-memory" / "memories.sqlite3"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        parser.error("parser promotion requires --confirm")
    try:
        payload = activate_parser_v2(
            args.database,
            root=args.root,
            project=args.project,
            backup=args.backup,
            allow_live=args.allow_live,
        )
    except (ParserActivationError, OSError, ValueError) as exc:
        payload = {"schema_version": "bhm.code-graph-parser-v2.activation.v1", "ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    write_report(payload, args.report)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
