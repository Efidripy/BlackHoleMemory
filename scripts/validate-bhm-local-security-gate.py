#!/usr/bin/env python3
"""Validate the local-only bulk security discovery/triage preflight contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from blackholememory.filesystem_boundaries import replace_bytes_safely  # noqa: E402
from blackholememory.local_security_gate import evaluate_local_security_gate  # noqa: E402
from blackholememory.local_security_gate import load_json_object  # noqa: E402


def _write_report(path: Path | None, rendered: str) -> None:
    if path is not None:
        replace_bytes_safely(path.expanduser(), rendered.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=ROOT / "config" / "security-scan-local-llm.json")
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--require-ready", action="store_true", help="fail unless an enabled policy is ready")
    args = parser.parse_args()

    try:
        policy = load_json_object(args.policy)
        attestation = load_json_object(args.attestation) if args.attestation else None
        result = evaluate_local_security_gate(policy, attestation)
    except Exception as exc:
        result = {
            "schema_version": "bhm.security.local-llm-gate.v1",
            "status": "blocked",
            "eligible": False,
            "reasons": [f"input_error:{type(exc).__name__}"],
            "detail": str(exc)[:240],
            "model_started": False,
            "runtime_flags_changed": False,
        }

    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _write_report(args.report, rendered)
    print(rendered, end="")
    if result.get("status") == "blocked":
        return 1
    if args.require_ready and result.get("status") != "ready":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
