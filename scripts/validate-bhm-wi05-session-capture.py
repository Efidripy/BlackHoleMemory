"""Deterministic offline WI-05 session capture exit validator."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
from blackholememory import app as bhm_app
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES
from blackholememory.session_capture import DISCLOSURE_LEVELS
from blackholememory.session_capture import build_session_capture_preview
from blackholememory.session_capture import verify_session_capture_digest


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "scripts" / "bhm-session-capture.py"
BENCHMARK_PATH = ROOT / "scripts" / "benchmark-bhm-wi05-session-capture.py"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
WI05_PROCESS_TIMEOUT_SECONDS = PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
WI05_EXPECTED_CORE_TOOL_COUNT = 35


def _fixture() -> dict[str, list[dict]]:
    observations = [
        {
            "eventId": "evt-current",
            "sessionId": "sess-1",
            "project": "fixture",
            "hookType": "tool.complete",
            "timestamp": "2026-07-16T11:59:00Z",
            "data": {"secret": "never-return", "result": "ok"},
            "recordSha256": "a" * 64,
        },
        {
            "eventId": "evt-current",
            "sessionId": "sess-1",
            "project": "fixture",
            "hookType": "tool.complete",
            "timestamp": "2026-07-16T11:59:00Z",
            "data": {"result": "duplicate"},
            "recordSha256": "b" * 64,
        },
        {
            "eventId": "evt-stale",
            "sessionId": "sess-1",
            "project": "fixture",
            "hookType": "tool.start",
            "timestamp": "2026-01-01T00:00:00Z",
            "data": {"transcript": "stale raw"},
            "recordSha256": "c" * 64,
        },
        {"eventId": "evt-cross", "sessionId": "sess-1", "project": "other", "timestamp": "2026-07-16T11:00:00Z"},
    ]
    sessions = [{"id": "session-record-1", "project": "fixture", "session_id": "sess-1", "title": "Fixture", "next": "gate", "metadata": {"session_id": "sess-1"}}]
    memories = [
        {"source_id": "mem-1", "project": "fixture", "memory_type": "decision", "content": "SQLite authority", "tags": ["architecture"], "updated_at": "2026-07-16T11:00:00Z"},
        {"source_id": "mem-2", "project": "fixture", "memory_type": "decision", "content": "SQLite authority", "tags": ["architecture"], "updated_at": "2026-01-01T00:00:00Z"},
        {"source_id": "mem-cross", "project": "other", "memory_type": "decision", "content": "cross project"},
    ]
    return {"observations": observations, "session_records": sessions, "memories": memories}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _api_hidden() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    route = routes.get("/bhm/session-capture/preview")
    return route is not None and getattr(route, "include_in_schema", False) is False


def _run_bounded_child(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run disposable WI-05 children with a finite wait."""

    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=WI05_PROCESS_TIMEOUT_SECONDS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    fixture = _fixture()
    before = _digest(fixture)
    preview = build_session_capture_preview(
        fixture["observations"],
        session_records=fixture["session_records"],
        memories=fixture["memories"],
        project="fixture",
        session_id="sess-1",
        disclosure="audit",
        token_budget=650,
        max_items=8,
        stale_days=30,
        now=NOW,
    )
    checks: dict[str, bool] = {}
    checks["schema_and_digest"] = preview["schema_version"] == "bhm.session-capture.v1" and verify_session_capture_digest(preview)
    checks["disclosure_levels"] = all("events" in build_session_capture_preview(fixture["observations"], project="fixture", session_id="sess-1", disclosure=level, now=NOW)["packet"] for level in DISCLOSURE_LEVELS)
    diagnostics = preview["packet"]["diagnostics"]
    checks["project_and_session_isolation"] = diagnostics["excluded_cross_project_count"] == 2 and all(item.get("session_id") in {"sess-1", None} for item in preview["packet"]["events"])
    checks["duplicate_and_stale_detection"] = diagnostics["duplicate_event_ids"] == ["evt-current"] and diagnostics["stale_event_count"] == 1 and diagnostics["duplicate_memory_groups"] == [["mem-1", "mem-2"]]
    checks["bounded_budget"] = preview["budget"]["estimated_tokens"] <= 650 and preview["budget"]["token_budget"] == 650
    checks["provenance_complete"] = bool(preview["packet"]["provenance"]["observation_event_ids"]) and all("source_ref" in item for item in preview["packet"]["events"] + preview["packet"]["memories"])
    serialized = json.dumps(preview, ensure_ascii=False, sort_keys=True)
    checks["redaction_and_no_raw_payload"] = preview["execution"]["raw_payload_returned"] is False and "never-return" not in serialized and "stale raw" not in serialized
    checks["reversible_forget_preview"] = preview["packet"]["forget_preview"]["preview_only"] is True and bool(preview["packet"]["forget_preview"]["undo_token_digest"])
    checks["read_only_and_public_mcp_unchanged"] = preview["execution"]["writes_sqlite"] is False and preview["execution"]["writes_qdrant"] is False and len(CORE_TOOL_NAMES) == WI05_EXPECTED_CORE_TOOL_COUNT
    checks["hidden_api"] = _api_hidden()
    after = _digest(fixture)
    checks["no_input_mutation"] = before == after

    with tempfile.TemporaryDirectory(prefix="bhm-wi05-validator-") as raw:
        temp = Path(raw)
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        cli_report = temp / "cli.json"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli = _run_bounded_child(
            [sys.executable, str(CLI_PATH), "--action", "preview", "--fixture", str(fixture_path), "--project", "fixture", "--session-id", "sess-1", "--disclosure", "audit", "--report", str(cli_report)],
            cwd=ROOT,
            env=env,
        )
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_smoke"] = cli.returncode == 0 and cli_payload.get("schema_version") == "bhm.session-capture.v1" and cli_payload.get("execution", {}).get("writes_sqlite") is False
        benchmark_report = temp / "benchmark.json"
        benchmark = _run_bounded_child(
            [sys.executable, str(BENCHMARK_PATH), "--items-per-source", "20", "--iterations", "8", "--report", str(benchmark_report)],
            cwd=ROOT,
            env=env,
        )
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["latency_benchmark"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True and benchmark_payload.get("checks", {}).get("p95_budget") is True
        details = {"response_digest": preview["response_digest"], "counts": preview["counts"], "budget": preview["budget"], "benchmark": benchmark_payload.get("latency", {})}

    failed = [name for name, value in checks.items() if not value]
    report = {
        "schema_version": "bhm.wi05.session-capture-validation.v1",
        "ok": not failed,
        "check_count": len(checks),
        "passed_count": len(checks) - len(failed),
        "checks": checks,
        "failed": failed,
        "details": details,
        "writes_live_state": False,
        "writes_qdrant": False,
        "model_started": False,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
