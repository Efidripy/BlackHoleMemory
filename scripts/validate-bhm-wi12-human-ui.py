"""Deterministic WI-12 human UI/Obsidian bridge exit validator."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
from blackholememory import app as bhm_app
from blackholememory.human_ui_bridge import build_human_ui_bridge_preview
from blackholememory.human_ui_bridge import verify_human_ui_bridge_digest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "bhm-human-ui.py"
BENCHMARK = ROOT / "scripts" / "benchmark-bhm-wi12-human-ui.py"
WI12_PROCESS_TIMEOUT_SECONDS = PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS


def _fixture() -> dict:
    nodes = [
        {"id": "memory::one", "label": "Architecture decision", "type": "memory", "project": "fixture", "confidence": 0.9, "source_ref": ".docs/adr/0001.md", "stale": False, "quarantined": False, "provenance": "adr"},
        {"id": "task::one", "label": "Task one", "type": "task", "project": "fixture", "confidence": 0.8, "source_ref": "task:one", "stale": True, "quarantined": False, "provenance": "task-graph"},
        {"id": "quarantine::one", "label": "Quarantine candidate", "type": "memory", "project": "fixture", "confidence": 0.2, "source_ref": "import:one", "stale": False, "quarantined": True, "provenance": "import"},
    ]
    note = {"entity_id": "memory::one", "title": "Architecture decision", "content": "SQLite remains authoritative.", "source_ref": ".docs/adr/0001.md", "confidence": 0.9}
    return {"project": "fixture", "nodes": nodes, "links": [{"source": "memory::one", "target": "task::one", "kind": "evidence_for", "confidence": 0.8}], "selected_id": "memory::one", "provenance": [{"entity_id": "memory::one", "source_ref": ".docs/adr/0001.md", "class": "adr"}], "review_items": [{"id": "review::one", "requires_human_review": True}], "task_items": [{"task_id": "task::one", "status": "open"}], "context_packet": {"token_usage": 120, "max_tokens": 1200, "truncated": False}, "mcp_state": {"server_id": "bhm", "status": "MCP unavailable"}, "obsidian_export": [note], "snapshot_id": "snapshot-fixture", "generated_at": "2026-07-16T00:00:00Z"}


def _hidden_api() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    route = routes.get("/bhm/human-ui/preview")
    return route is not None and route.include_in_schema is False


def _run_bounded_child(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run disposable WI-12 children with a finite wait."""

    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=WI12_PROCESS_TIMEOUT_SECONDS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    fixture = _fixture()
    preview = build_human_ui_bridge_preview(**fixture)
    export_note = preview["obsidian_export"]["notes"][0]
    accepted = build_human_ui_bridge_preview(**{**fixture, "obsidian_import": [{"entity_id": export_note["entity_id"], "title": export_note["title"], "content": "SQLite remains authoritative.", "frontmatter": export_note["frontmatter"]}]})
    conflict = build_human_ui_bridge_preview(**{**fixture, "obsidian_import": [{"entity_id": export_note["entity_id"], "title": export_note["title"], "content": "tampered", "frontmatter": export_note["frontmatter"]}]})
    unmarked = build_human_ui_bridge_preview(**{**fixture, "obsidian_import": [{"entity_id": "x", "content": "unmarked", "frontmatter": {}}]})
    checks = {
        "schema_digest": preview["schema_version"] == "bhm.human-ui-bridge.v1" and verify_human_ui_bridge_digest(preview),
        "bounded_graph": preview["checks"]["bounded_graph"] and len(preview["graph"]["nodes"]) == 3 and len(preview["graph"]["links"]) == 1,
        "selected_provenance": preview["graph"]["selected"]["source_ref"] == ".docs/adr/0001.md" and preview["checks"]["selected_provenance_explainable"],
        "stale_quarantine_visible": preview["checks"]["stale_quarantine_visible"] and any(item["quarantined"] for item in preview["graph"]["nodes"]),
        "context_and_review_panels": bool(preview["review_queue"]) and isinstance(preview["context_packet"], dict) and "token_usage" in preview["context_packet"],
        "obsidian_export_contract": export_note["readonly"] is True and export_note["frontmatter"]["bhm_marker"] == "bhm:readonly:v1" and export_note["frontmatter"]["bhm_checksum"] == export_note["checksum"],
        "obsidian_import_accepts_marked": accepted["obsidian_import"]["accepted"] and not accepted["obsidian_import"]["conflicts"],
        "checksum_conflict_preview": bool(conflict["obsidian_import"]["conflicts"]) and conflict["obsidian_import"]["conflicts"][0]["reason"] == "checksum_mismatch",
        "unmarked_rejected": bool(unmarked["obsidian_import"]["rejected"]),
        "no_authority_write": all(value is False for value in preview["execution"].values() if isinstance(value, bool)) and preview["checks"]["no_authority_write"],
        "hidden_api_and_static_ui": _hidden_api() and (ROOT / "src" / "blackholememory" / "static" / "galaxy.html").exists(),
        "cli_smoke": False,
        "benchmark": False,
    }
    with tempfile.TemporaryDirectory(prefix="bhm-wi12-validator-") as raw:
        temp = Path(raw)
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli_report = temp / "cli.json"
        cli = _run_bounded_child([sys.executable, str(CLI), "--fixture", str(fixture_path), "--report", str(cli_report)], cwd=ROOT, env=env)
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_smoke"] = cli.returncode == 0 and cli_payload.get("ui_digest") == preview.get("ui_digest")
        benchmark_report = temp / "benchmark.json"
        benchmark = _run_bounded_child([sys.executable, str(BENCHMARK), "--iterations", "16", "--p95-budget-ms", "250", "--report", str(benchmark_report)], cwd=ROOT, env=env)
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["benchmark"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True
        details = {"ui_digest": preview["ui_digest"], "obsidian_export_digest": preview["obsidian_export"]["digest"], "benchmark": benchmark_payload.get("latency", {})}
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi12.human-ui-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "obsidian_committed": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
