"""Preview the WI-15 fail-closed security and trust boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blackholememory.security_trust_boundary import build_security_trust_boundary_preview


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
    items = payload.pop("items", [])
    preview = build_security_trust_boundary_preview(items, **payload)
    rendered = json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = args.report.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if all(bool(value) for value in preview["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
