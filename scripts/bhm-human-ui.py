"""Preview the bounded WI-12 human UI and optional Obsidian bridge contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blackholememory.human_ui_bridge import build_human_ui_bridge_preview


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload: dict[str, Any] = {}
    if args.fixture:
        payload = json.loads(args.fixture.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("fixture root must be an object")
    payload.setdefault("project", args.project)
    preview = build_human_ui_bridge_preview(**payload)
    rendered = json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = args.report.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if all(preview["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
