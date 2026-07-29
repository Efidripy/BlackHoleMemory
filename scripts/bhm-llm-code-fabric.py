"""Explicit WI-09 proposal-only local LLM code-fabric CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.llm_code_fabric import LLM_CODE_FABRIC_SCHEMA_VERSION
from blackholememory.llm_code_fabric import LLM_CODE_FABRIC_TASKS
from blackholememory.llm_code_fabric import LLMCodeFabricError
from blackholememory.llm_code_fabric import build_code_fabric_plan


def _emit(value: object, report: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(rendered)
    if report:
        target = Path(report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "preview"), default="preview")
    parser.add_argument("--task-type", choices=LLM_CODE_FABRIC_TASKS, default="code_summary")
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--payload-file")
    parser.add_argument("--context-digest", default="")
    parser.add_argument("--required-capabilities", nargs="*", default=["json"])
    parser.add_argument("--context-tokens", type=int, default=8_192)
    parser.add_argument("--sensitivity", choices=("public", "internal", "restricted"), default="internal")
    parser.add_argument("--mutation-requested", action="store_true")
    parser.add_argument("--confidence", type=float, default=0.8)
    parser.add_argument("--evidence-count", type=int, default=1)
    parser.add_argument("--risk-flags", nargs="*", default=[])
    parser.add_argument("--operator-approved", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        if args.action == "plan":
            _emit({"schema_version": LLM_CODE_FABRIC_SCHEMA_VERSION, "ok": True, "action": "plan", "tasks": list(LLM_CODE_FABRIC_TASKS), "execution_enabled": False, "auto_apply": False, "model_started": False, "writes_sqlite": False}, args.report)
            return 0
        payload = {}
        if args.payload_file:
            payload = json.loads(Path(args.payload_file).expanduser().resolve().read_text(encoding="utf-8"))
        result = build_code_fabric_plan(args.task_type, payload, project=args.project, context_digest=args.context_digest, required_capabilities=args.required_capabilities, context_tokens=args.context_tokens, sensitivity=args.sensitivity, mutation_requested=args.mutation_requested, confidence=args.confidence, evidence_count=args.evidence_count, risk_flags=args.risk_flags, operator_approved=args.operator_approved)
        _emit(result, args.report)
        return 0
    except (LLMCodeFabricError, OSError, ValueError, json.JSONDecodeError) as exc:
        _emit({"schema_version": LLM_CODE_FABRIC_SCHEMA_VERSION, "ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]}, args.report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
