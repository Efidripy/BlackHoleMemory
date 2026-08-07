"""Print a bounded REST/MCP schema parity inventory."""

from __future__ import annotations

import json

from blackholememory.rest_mcp_parity import build_rest_mcp_parity_inventory


def main() -> int:
    report = build_rest_mcp_parity_inventory()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
