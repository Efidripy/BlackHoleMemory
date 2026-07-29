#!/usr/bin/env python3
"""Capture truthful dispositions for CBM live activation, migration and continuity."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _probe(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            return response.status == 200
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads((ROOT / "config" / "cbm-integration.json").read_text(encoding="utf-8"))
    flags = config.get("feature_flags", {})
    all_disabled = all(value is False for value in flags.values())
    migration = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate-bhm-wi14-migration.py")],
        cwd=ROOT,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    migration_ok = migration.returncode == 0
    report = {
        "schema_version": "bhm.p21.10-12.activation-dispositions.v1",
        "p21_10_live_activation": {
            "disposition": "rejected_current_scope",
            "status": "not-activated-by-policy",
            "flags_all_false": all_disabled,
            "reason": "CBM preview surfaces have no live repository snapshot/graph/convention authority; enabling them would create unreviewed persistent derived state",
            "risk": "implemented previews remain unavailable through live routes until a separately approved canary",
            "decision": "keep feature flags false; retain proposal-only and SQLite-authoritative invariants",
        },
        "p21_11_migration": {
            "disposition": "rehearsed-not-applied",
            "status": "dry-run-green" if migration_ok else "dry-run-failed",
            "validator_exit": migration.returncode,
            "reason": "WI-14 migration compatibility is exercised as an isolated dry-run; live apply requires operator approval",
            "risk": "live migration remains unproven and is intentionally not performed",
            "decision": "retain append-only staging, backup/rollback passport and no authority mutation",
        },
        "p21_12_continuity": {
            "disposition": "rejected_current_scope",
            "status": "deferred-not-live",
            "reason": "the required task→live graph→context→proposal→review→checkpoint chain cannot be claimed while P21.10 live activation is intentionally disabled",
            "risk": "cross-session continuity of derived CBM surfaces remains unmeasured",
            "decision": "keep synthetic/offline continuity tests as evidence and require a new activation Plan ID for live canary",
        },
        "runtime_canary": {
            "ready": _probe("http://127.0.0.1:8000/health/ready"),
            "slo": _probe("http://127.0.0.1:8000/bhm/health/slo"),
            "cutover": _probe("http://127.0.0.1:8000/health/cutover"),
        },
        "rollback": "restore the prior flag file; no live CBM activation or data mutation occurred",
        "final_integrator": "codex:/root",
    }
    report["checks"] = {"flags_truthful": all_disabled, "migration_rehearsal": migration_ok, "runtime_core_healthy": all(report["runtime_canary"].values()), "dispositions_explicit": True}
    report["ok"] = all(bool(value) for value in report["checks"].values())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
