"""Run one canonical BHM integration gate with optional domain selection."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = REPO_ROOT / "tests" / "integration"
sys.path.insert(0, str(REPO_ROOT))

from tests.integration.domain_manifest import DOMAIN_NAMES  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--domain",
        default="all",
        choices=("all", *DOMAIN_NAMES),
        help="all or one registered BHM integration domain marker",
    )
    parser.add_argument("--junitxml", type=Path, help="optional pytest JUnit output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pytest_args = ["-q", str(TEST_ROOT)]
    if args.domain != "all":
        pytest_args.extend(["-m", f"bhm_{args.domain}"])
    if args.junitxml:
        args.junitxml.parent.mkdir(parents=True, exist_ok=True)
        pytest_args.append(f"--junitxml={args.junitxml}")
    print(f"BHM integration gate: domain={args.domain}; root={TEST_ROOT}")
    import pytest

    return int(pytest.main(pytest_args))


if __name__ == "__main__":
    sys.exit(main())
