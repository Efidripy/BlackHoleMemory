#!/usr/bin/env python
"""Validate the pinned Qdrant image and loopback-only host bindings."""

from __future__ import annotations

# The script adds the repository's src directory before importing project modules.
# ruff: noqa: E402

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.qdrant_runtime import validate_qdrant_runtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compose",
        type=Path,
        default=REPO_ROOT / "infra" / "qdrant" / "docker-compose.yml",
        help="Qdrant Docker Compose file to inspect",
    )
    parser.add_argument(
        "--launcher",
        type=Path,
        default=REPO_ROOT / "scripts" / "bhm_launcher.py",
        help="Operator launcher containing the image pull command",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    compose_path = args.compose.resolve()
    launcher_path = args.launcher.resolve()
    report = validate_qdrant_runtime(
        compose_path.read_text(encoding="utf-8"),
        launcher_text=launcher_path.read_text(encoding="utf-8"),
    )
    report["compose"] = str(compose_path)
    report["launcher"] = str(launcher_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
