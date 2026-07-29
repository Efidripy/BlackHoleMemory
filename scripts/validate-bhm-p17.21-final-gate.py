"""Executable deterministic final gate for P17.21."""

from __future__ import annotations

import json

from blackholememory.llm_final_gate import run_final_gate


def main() -> int:
    report = run_final_gate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
