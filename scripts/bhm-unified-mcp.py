"""Preview the deterministic WI-11 unified MCP/hooks/adapter contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blackholememory.unified_mcp_contract import build_unified_mcp_contract


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("preview",), default="preview")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload: dict[str, Any] = {}
    if args.fixture:
        payload = json.loads(args.fixture.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("fixture root must be an object")
    contract = build_unified_mcp_contract(
        manifest_path=args.manifest or payload.get("manifest_path"),
        initialize_response=payload.get("initialize_response"),
        catalog_response=payload.get("catalog_response"),
        client_snapshots=payload.get("client_snapshots"),
        native_mcp=payload.get("native_mcp"),
        hook_profile=payload.get("hook_profile"),
    )
    rendered = json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = args.report.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    active_checks = {
        name: value
        for name, value in contract["checks"].items()
        if name != "public_core_12_tools"
    }
    return 0 if all(active_checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
