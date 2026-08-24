#!/usr/bin/env python3
"""Validate explicit tool-use, non-use and limitation evidence for P21.19."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.filesystem_boundaries import replace_bytes_safely


ENTRIES = [
    {"tool": "BHM runtime", "status": "used", "action": "runtime health and evidence/checkpoint path", "evidence": [".docs/ops/bhm-p21.0-r2-mcp-sdk-compatibility-2026-07-21.md"]},
    {"tool": "Native MCP attach", "status": "limited", "action": "active attach kept separate from configured inventory; no false attach claim", "evidence": [".docs/ops/bhm-p21.0-r1-connector-http-truth-2026-07-17.md"]},
    {"tool": "BHM plugin/hooks", "status": "used", "action": "unified MCP, hooks, adapter and rollback contracts", "evidence": [".docs/ops/bhm-p20.11-wi11-unified-mcp-2026-07-16.md", "plugins/bhm-codex-connector/README.md"]},
    {"tool": "Shell and validators", "status": "used", "action": "read-only gates, source passport, P21.13–P21.18 evidence validators", "evidence": ["scripts/validate-bhm-source-passport.py", "scripts/validate-bhm-source-freeze.py"]},
    {"tool": "Subagents", "status": "used", "action": "bounded independent gate, source and release audits with /root final integration", "evidence": [".docs/ops/bhm-p21.17-wi35-source-delta-2026-07-21.md"]},
    {"tool": "Codex Security", "status": "used", "action": "official deep scan sealed; no repeated unchanged-snapshot scan", "evidence": [".docs/ops/bhm-p21.1-wi19-r3-review-changes-sealed-2026-07-21.md"]},
    {"tool": "Local LLM security worker", "status": "limited", "action": "policy and gate validated, execution remains disabled; routine 25/10 reserved for local worker, final 100/50 only at P21.20", "evidence": [".docs/adr/0185-p21.1-local-security-worker-contract.md", "config/security-scan-local-llm.json"]},
    {"tool": "Playwright", "status": "used", "action": "UI/MCP panel smoke artifacts", "evidence": ["output/playwright/p21.0-r1-mcp-galaxy.png", "output/playwright/p21.0-r1-mcp-workbench.png"]},
    {"tool": "Browser control", "status": "not-required", "action": "no external authenticated browser session or web research was required for this local change stream", "evidence": [".docs/ops/bhm-p21.17-wi35-source-delta-2026-07-21.md"]},
    {"tool": "Figma", "status": "not-required", "action": "no Figma design input was in scope; no style was guessed or modified", "evidence": [".docs/ops/bhm-p21.18-wi36-source-freeze-2026-07-21.md"]},
    {"tool": "n8n", "status": "not-required", "action": "no webhook/notification workflow was needed; local validators are the canonical path", "evidence": [".docs/ops/bhm-p21.18-wi36-source-freeze-2026-07-21.md"]},
    {"tool": ".src quarantine", "status": "used", "action": "manifest/hash/provenance boundary and source freeze", "evidence": [".docs/ops/bhm-p21.18-wi36-source-freeze-2026-07-21.md", "scripts/verify-local-source-boundary.ps1"]},
]


def _write_report(path: Path, report: dict) -> None:
    replace_bytes_safely(path, (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    failures = []
    for entry in ENTRIES:
        missing = [path for path in entry["evidence"] if not Path(path).exists()]
        entry["evidence_exists"] = not missing
        if missing:
            failures.append({"tool": entry["tool"], "missing": missing})
    statuses = {entry["status"] for entry in ENTRIES}
    report = {
        "schema_version": "bhm.p21.19.wi37.toolchain-ledger.v1",
        "generated_at": "2026-07-21",
        "plan_id": "BHM-V5-POST-ACCEPTANCE-20260717",
        "entries": ENTRIES,
        "status_counts": {status: sum(1 for entry in ENTRIES if entry["status"] == status) for status in sorted(statuses)},
        "silent_skips": [],
        "final_integrator": "Codex /root",
        "writes_live_state": False,
        "failures": failures,
        "ok": not failures and not reportable_silent_skip(ENTRIES),
    }
    _write_report(args.report, report)
    print(json.dumps({"ok": report["ok"], "entries": len(ENTRIES), "failures": failures, "silent_skips": report["silent_skips"]}, ensure_ascii=False))
    return 0 if report["ok"] else 1


def reportable_silent_skip(entries: list[dict]) -> bool:
    return any(entry["status"] == "skipped" for entry in entries)


if __name__ == "__main__":
    raise SystemExit(main())
