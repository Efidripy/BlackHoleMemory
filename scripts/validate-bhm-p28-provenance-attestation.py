"""Validate an operator provenance-attestation envelope read-only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blackholememory.provenance_attestation import build_provenance_attestation_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--attestation", type=Path, required=True)
    parser.add_argument("--package", action="append", type=Path, default=[])
    parser.add_argument("--sbom", action="append", type=Path, default=[])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = build_provenance_attestation_report(args.repo, args.attestation, package_paths=args.package, sbom_paths=args.sbom)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0 if report["state"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
