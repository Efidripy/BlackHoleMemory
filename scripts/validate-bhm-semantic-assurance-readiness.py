#!/usr/bin/env python
"""Verify a local, content-free semantic-assurance readiness evidence package.

The command only reads JSON files and writes an optional derived receipt.  It
never starts a model, contacts a service, opens BHM storage, or authorises an
automatic lifecycle action.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.semantic_assurance_readiness import SemanticAssuranceReadinessError
from blackholememory.semantic_assurance_readiness import load_semantic_assurance_readiness
from blackholememory.semantic_assurance_readiness import write_semantic_assurance_readiness_receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True, help="local content-free readiness evidence JSON")
    parser.add_argument("--output", type=Path, help="optional derived content-free readiness receipt JSON")
    args = parser.parse_args(argv)
    try:
        receipt = load_semantic_assurance_readiness(args.evidence)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SemanticAssuranceReadinessError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    if args.output:
        write_semantic_assurance_readiness_receipt(receipt, args.output)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
