#!/usr/bin/env python
"""Admit one local private semantic-assurance corpus for blinded human review.

This command reads one local JSON input and emits only a content-free receipt.
It never calls a model or network service, opens BHM authority storage, or
authorises lifecycle mutation or automatic semantic apply.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.semantic_assurance_corpus_admission import (
    SemanticAssuranceCorpusAdmissionError,
    admit_semantic_assurance_corpus_for_human_review,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="restricted local private corpus JSON")
    parser.add_argument("--output", type=Path, help="optional content-free admission receipt JSON")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        receipt = admit_semantic_assurance_corpus_for_human_review(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SemanticAssuranceCorpusAdmissionError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    rendered = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
