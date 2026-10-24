#!/usr/bin/env python
"""Audit private V6 curator inputs against explicit local canonical sources.

The command emits a content-free structural receipt only.  It does not create
reviewer packets, call a model or network service, open BHM storage, or permit
automatic semantic apply.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.semantic_assurance_curator_audit import (
    SemanticAssuranceCuratorAuditError,
    audit_v6_curator_inputs,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atomic-facts", type=Path, required=True, help="restricted private curator ledger JSON")
    parser.add_argument("--control-cells", type=Path, required=True, help="restricted private curator controls JSON")
    parser.add_argument("--canonical-root", type=Path, required=True, help="local canonical source root")
    parser.add_argument("--output", type=Path, help="optional content-free audit receipt JSON")
    args = parser.parse_args(argv)
    try:
        atomic_facts = json.loads(args.atomic_facts.read_text(encoding="utf-8"))
        control_cells = json.loads(args.control_cells.read_text(encoding="utf-8"))
        receipt = audit_v6_curator_inputs(atomic_facts, control_cells, canonical_root=args.canonical_root)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SemanticAssuranceCuratorAuditError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    rendered = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
