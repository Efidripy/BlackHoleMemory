#!/usr/bin/env python
"""Run WL-295.3's offline hybrid retrieval evaluation and write a local receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.hybrid_retrieval_evaluation import evaluate_hybrid_retrieval  # noqa: E402


RUNTIME_ROOT = REPO_ROOT / ".runtime" / "hybrid-retrieval-evaluation"


def _output_path(path: Path) -> Path:
    root = RUNTIME_ROOT.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"output must be below {root}") from exc
    if resolved.suffix.casefold() != ".json":
        raise ValueError("output must use a .json extension")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=11)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate_hybrid_retrieval(case_count=args.cases, repeats=args.repeats)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = _output_path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
