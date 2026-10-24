"""Run the canonical bounded read-only BHM MCP Doctor."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.mcp_doctor import DEFAULT_BASE_URL
from blackholememory.mcp_doctor import DoctorConfig
from blackholememory.mcp_doctor import run_doctor


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--codex-config", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    report = run_doctor(
        DoctorConfig(
            base_url=args.base_url,
            repo_root=args.repo_root,
            manifest=args.manifest,
            codex_config=args.codex_config,
            timeout_seconds=args.timeout_seconds,
        )
    )
    if args.compact:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
