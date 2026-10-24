"""Explicit WI-10 QA/incident/documentation factory integration CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.factory_integration import FACTORY_INTEGRATION_SCHEMA_VERSION
from blackholememory.factory_integration import FactoryIntegrationError
from blackholememory.factory_integration import build_factory_integration_preview


def _emit(value: object, report: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(rendered)
    if report:
        target = Path(report).expanduser()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "preview"), default="preview")
    parser.add_argument("--fixture")
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--risk-class", choices=("low", "medium", "high", "critical"), default="medium")
    parser.add_argument("--max-items", type=int, default=32)
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        if args.action == "plan":
            _emit({"schema_version": FACTORY_INTEGRATION_SCHEMA_VERSION, "ok": True, "action": "plan", "factories": ["qa", "incident", "documentation"], "execution_enabled": False, "auto_apply": False, "tests_started": False}, args.report)
            return 0
        fixture = {"artifacts": [], "documents": [], "changed_paths": [], "code_items": [], "task_items": []}
        if args.fixture:
            payload = json.loads(Path(args.fixture).expanduser().resolve().read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise FactoryIntegrationError("fixture must be an object")
            fixture.update({key: payload.get(key) or fixture[key] for key in fixture})
        result = build_factory_integration_preview(fixture["artifacts"], fixture["documents"], project=args.project, changed_paths=fixture["changed_paths"], code_items=fixture["code_items"], task_items=fixture["task_items"], risk_class=args.risk_class, max_items=args.max_items)
        _emit(result, args.report)
        return 0
    except (FactoryIntegrationError, OSError, ValueError, json.JSONDecodeError) as exc:
        _emit({"schema_version": FACTORY_INTEGRATION_SCHEMA_VERSION, "ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]}, args.report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
