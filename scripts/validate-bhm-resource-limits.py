"""Validate the deterministic BHM resource-limit inventory."""

from __future__ import annotations

import json

from blackholememory.resource_limits import resource_limit_snapshot


def main() -> int:
    snapshot = resource_limit_snapshot()
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    return 0 if snapshot["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
