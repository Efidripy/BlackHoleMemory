"""Plan or explicitly apply the bounded legacy JSON-sidecar reconciliation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from blackholememory.sidecar_reconciliation import SidecarReconciliationError
from blackholememory.sidecar_reconciliation import apply_reconciliation_plan
from blackholememory.sidecar_reconciliation import build_reconciliation_plan


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--runtime-dir", type=Path, default=root / ".runtime" / "live-memory")
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--plan-out", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-digest")
    parser.add_argument("--allow-live", action="store_true", help="required when applying to the selected runtime directory")
    args = parser.parse_args()
    try:
        runtime = args.runtime_dir.resolve()
        plan = build_reconciliation_plan(runtime)
        if args.plan_out:
            args.plan_out.parent.mkdir(parents=True, exist_ok=True)
            args.plan_out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result: dict[str, object] = {"plan": plan}
        if args.apply:
            database = (args.database or runtime / "memories.sqlite3").resolve()
            if not args.confirm_plan_digest or args.confirm_plan_digest != plan["plan_digest"]:
                raise SidecarReconciliationError("--confirm-plan-digest must equal the freshly built plan digest")
            if database == (runtime / "memories.sqlite3").resolve() and not args.allow_live:
                raise SidecarReconciliationError("--allow-live is required for the selected runtime database")
            result["apply"] = apply_reconciliation_plan(database, plan)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except SidecarReconciliationError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
