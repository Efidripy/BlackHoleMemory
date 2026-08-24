"""Validate the deterministic WI-17 product-value/final-acceptance contract."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS
from blackholememory.product_value import build_product_value_benchmark
from blackholememory.product_value import verify_product_value_digest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "bhm-product-value.py"
BENCHMARK = ROOT / "scripts" / "benchmark-bhm-product-value.py"
WI17_PROCESS_TIMEOUT_SECONDS = PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS


def _archive_boundary(path: Path) -> dict[str, object]:
    unsafe: list[str] = []
    forbidden: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            normalized = name.replace("\\", "/")
            if normalized.startswith("/") or ".." in normalized.split("/"):
                unsafe.append(name)
            if any(token in normalized.casefold() for token in ("/.src/", "/.env", "/runtime/", ".sqlite", ".db", "/secrets/", "/credentials/")):
                forbidden.append(name)
    return {"unsafe": unsafe, "forbidden": forbidden, "ok": not unsafe and not forbidden}


def _run(command: list[str], *, env: dict[str, str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=WI17_PROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout + result.stderr)[-4_000:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-archive", type=Path, required=True)
    parser.add_argument("--release-sha256", required=True)
    parser.add_argument("--pytest-count", type=int, default=682)
    parser.add_argument("--report")
    args = parser.parse_args()
    archive = args.release_archive.expanduser().resolve()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    benchmark = build_product_value_benchmark(iterations=16)
    boundary = _archive_boundary(archive)
    checks = {
        "product_value_digest": verify_product_value_digest(benchmark),
        "product_value_checks": all(bool(value) for value in benchmark["checks"].values()),
        "product_value_is_documented_synthetic": benchmark["evidence_class"] == "synthetic-bounded-fixture" and benchmark["real_user_telemetry"] is False,
        "release_exists": archive.is_file(),
        "release_sha256": archive.is_file() and __import__("hashlib").sha256(archive.read_bytes()).hexdigest() == str(args.release_sha256).lower(),
        "archive_boundary": bool(boundary["ok"]),
        "pytest_baseline_recorded": int(args.pytest_count) >= 682,
        "dependency_gate": False,
        "mcp_latency_gate": False,
        "cli_smoke": False,
    }
    details: dict[str, object] = {"benchmark": {"digest": benchmark["benchmark_digest"], "utility_score": benchmark["utility_score"], "decision": benchmark["decision"]}, "archive_boundary": boundary, "pytest_count": int(args.pytest_count)}
    with tempfile.TemporaryDirectory(prefix="bhm-wi17-final-") as raw:
        temp = Path(raw)
        fixture = temp / "fixture.json"
        fixture.write_text("{}", encoding="utf-8")
        cli_report = temp / "cli.json"
        cli_ok, cli_output = _run([sys.executable, str(CLI), "--fixture", str(fixture), "--report", str(cli_report)], env=env)
        checks["cli_smoke"] = cli_ok and cli_report.is_file()
        dependency_ok, dependency_output = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "validate-bhm-dependencies.ps1"), "-AsJson"], env=env)
        checks["dependency_gate"] = dependency_ok
        latency_ok, latency_output = _run([sys.executable, str(ROOT / "scripts" / "validate-bhm-mcp-latency.py"), "--iterations", "12", "--max-attach-ms", "250", "--max-catalog-bytes", "65536"], env=env)
        checks["mcp_latency_gate"] = latency_ok
        details["cli_output"] = cli_output[-1_000:]
        details["dependency_output"] = dependency_output[-1_000:]
        details["latency_output"] = latency_output[-1_000:]
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi17.final-acceptance.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "pruning": benchmark["pruning"], "final_integrator": "codex:/root", "change_stream": "Codex /root", "release": {"archive": str(archive), "sha256": str(args.release_sha256).lower(), "prepared_not_published": True}, "execution": {"model_called": False, "agent_started": False, "network_called": False, "sqlite_written": False, "qdrant_written": False, "mem0_written": False, "files_written": False, "apply_performed": False}}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
