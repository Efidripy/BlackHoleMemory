"""Validate bounded proposal-only cross-repository Git history joins."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from blackholememory.change_impact import build_cross_repo_history_preview  # noqa: E402
from blackholememory.filesystem_boundaries import replace_bytes_safely  # noqa: E402


def run() -> dict:
    evidence = [
        {"project": "alpha", "history": {"commits_considered": 3, "hotspots": [{"path": "src/service.py", "commits": 2}]}, "symbols": [{"qualified_name": "service.route", "stable_key": "a:route"}]},
        {"project": "beta", "history": {"commits_considered": 2, "hotspots": [{"path": "lib/service.py", "commits": 1}]}, "symbols": [{"qualified_name": "service.route", "stable_key": "b:route"}]},
    ]
    first = build_cross_repo_history_preview(evidence)
    second = build_cross_repo_history_preview(evidence)
    return {
        "schema_version": first["schema_version"],
        "proposal_count": len(first["proposals"]),
        "relations": sorted({item["relation"] for item in first["proposals"]}),
        "deterministic": first["preview_digest"] == second["preview_digest"],
        "preview_digest": first["preview_digest"],
        "provenance": first["provenance"],
        "execution": first["execution"],
        "bounds": first["bounds"],
        "ok": bool(first["proposals"] and first["provenance"]["authority"] == "proposal" and not first["execution"]["cross_edges_promoted"] and first["execution"]["writes_sqlite_state"] is False),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run()
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        replace_bytes_safely(args.output, payload.encode("utf-8"))
    print(payload, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
