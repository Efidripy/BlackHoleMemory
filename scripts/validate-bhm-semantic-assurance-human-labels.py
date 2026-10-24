#!/usr/bin/env python
"""Validate a local dual-review semantic-assurance label manifest.

The command is local-only.  It writes or prints a content-free admission
receipt and never invokes a model or BHM authority surface.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_dataset
from blackholememory.semantic_assurance_label_admission import SemanticAssuranceLabelAdmissionError
from blackholememory.semantic_assurance_label_admission import admit_semantic_assurance_human_labels


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO_ROOT / "tests" / "fixtures" / "semantic-assurance-v1.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, required=True, help="restricted local human-label JSON manifest")
    parser.add_argument("--output", type=Path, help="optional content-free receipt JSON")
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        receipt = admit_semantic_assurance_human_labels(manifest, load_semantic_assurance_dataset(args.dataset))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SemanticAssuranceLabelAdmissionError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    if args.output:
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
