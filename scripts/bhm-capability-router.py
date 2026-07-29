"""Preview the deterministic WI-13 capability route plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blackholememory.capability_router import build_capability_route_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-type", default="retrieval")
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--scope", default="repository")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--local-model", default="qwen2.5-coder-7b-instruct")
    parser.add_argument("--local-capabilities", nargs="*", default=["classification", "coding", "reasoning", "json"])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload: dict[str, Any] = {}
    if args.fixture:
        payload = json.loads(args.fixture.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("fixture root must be an object")
    task_type = str(payload.pop("task_type", args.task_type))
    payload.setdefault("project", args.project)
    payload.setdefault("scope", args.scope)
    payload.setdefault("local_capabilities", [task_type, *args.local_capabilities])
    payload.setdefault(
        "models",
        [
            {
                "model_id": args.local_model,
                "capabilities": args.local_capabilities,
                "context_window": 131_072,
                "local_only": True,
                "available": True,
                "latency_ms": 788.0,
            }
        ],
    )
    payload.setdefault("measurements", [{"context_tokens": 8192, "ok": True, "latency_ms": 788.0, "source": "P24.1-local-llm-proof"}])
    plan = build_capability_route_plan(task_type, **payload)
    rendered = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = args.report.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if all(plan["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
