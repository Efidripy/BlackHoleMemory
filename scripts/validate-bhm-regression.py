"""Validate P15 API, schema, startup, latency and coverage contracts."""

from __future__ import annotations

# The baseline builder is loaded from the repository and the subprocesses are
# intentionally bounded/read-only.  No memory, Qdrant or source mutation is
# performed by this gate.
# ruff: noqa: E402

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from bhm_runtime_endpoints import endpoint_url
except ModuleNotFoundError:
    from scripts.bhm_runtime_endpoints import endpoint_url


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.resource_limits import PROCESS_EXECUTION_P15_LATENCY_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_P15_STARTUP_TIMEOUT_SECONDS


BASELINE_BUILDER = REPO_ROOT / "scripts" / "build-bhm-p15-dependency-baseline.py"
LATENCY_SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-mcp-latency.py"
START_SCRIPT = REPO_ROOT / "scripts" / "start-bhm-authoritative.ps1"
DEFAULT_BASELINE = REPO_ROOT / "docs" / "ops" / "bhm-p15.1-dependency-baseline-2026-07-14.json"


def load_baseline_builder():
    spec = importlib.util.spec_from_file_location("bhm_p15_dependency_baseline", BASELINE_BUILDER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load baseline builder: {BASELINE_BUILDER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def run_startup_probe() -> dict[str, Any]:
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(START_SCRIPT),
        "-ProbeOnly",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PROCESS_EXECUTION_P15_STARTUP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "exit_code": None, "error": str(exc)}
    payload: dict[str, Any] = {}
    try:
        decoded = json.loads(result.stdout)
        if isinstance(decoded, dict):
            payload = decoded
    except json.JSONDecodeError:
        payload = {}
    return {
        "ok": result.returncode == 0 and payload.get("ok") is True,
        "exit_code": result.returncode,
        "reported": payload,
        "stderr": result.stderr.strip(),
    }


def run_latency(iterations: int) -> dict[str, Any]:
    command = [sys.executable, str(LATENCY_SCRIPT), "--iterations", str(iterations)]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PROCESS_EXECUTION_P15_LATENCY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "exit_code": None, "error": str(exc)}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "exit_code": result.returncode, "error": result.stdout[-1000:]}
    return {"ok": result.returncode == 0 and payload.get("ok") is True, "exit_code": result.returncode, "report": payload}


def compare_contracts(
    baseline: dict[str, Any],
    current: dict[str, Any],
    coverage: dict[str, Any],
    startup: dict[str, Any],
    latency: dict[str, Any],
    *,
    coverage_floor: float = 65.0,
) -> dict[str, Any]:
    failures: list[str] = []
    checks: dict[str, bool] = {}

    checks["semantic_route_count"] = current.get("route_count") == baseline.get("route_count")
    checks["semantic_routes_digest"] = current.get("routes_digest") == baseline.get("routes_digest")
    checks["mcp_count"] = current.get("mcp_catalog", {}).get("count") == baseline.get("mcp_catalog", {}).get("count")
    checks["mcp_digest"] = current.get("mcp_catalog", {}).get("digest") == baseline.get("mcp_catalog", {}).get("digest")
    checks["import_cycles"] = current.get("import_cycles") == []

    baseline_api = baseline.get("api_behavior", {})
    current_api = current.get("api_behavior", {})
    baseline_openapi = baseline_api.get("/openapi.json", {})
    current_openapi = current_api.get("/openapi.json", {})
    checks["openapi_digest"] = current_openapi.get("digest") == baseline_openapi.get("digest")
    checks["openapi_shape"] = all(
        current_openapi.get(key) == baseline_openapi.get(key)
        for key in ("path_count", "operation_count", "schema_count", "version")
    )

    health = current_api.get("/bhm/health", {})
    cutover = current_api.get("/health/cutover", {})
    slo = current_api.get("/bhm/health/slo", {})
    checks["health"] = health.get("ok") is True and health.get("status") == "healthy" and health.get("memory_store") == "sqlite-authoritative"
    checks["cutover"] = cutover.get("ok") is True and cutover.get("graph") == "compiled" and cutover.get("memory_store") == "sqlite-authoritative"
    checks["slo"] = slo.get("ok") is True and slo.get("status") == "healthy" and slo.get("projection_pending") == 0 and slo.get("projection_failed") == 0
    checks["startup_probe"] = startup.get("ok") is True
    checks["latency"] = latency.get("ok") is True

    totals = coverage.get("totals", {})
    coverage_value = float(totals.get("percent_covered", 0.0))
    checks["coverage_floor"] = coverage_value >= coverage_floor

    for name, passed in checks.items():
        if not passed:
            failures.append(name)

    return {
        "ok": not failures,
        "failures": failures,
        "checks": checks,
        "coverage": {"percent_covered": coverage_value, "floor": coverage_floor},
        "startup": startup,
        "latency": latency,
        "current": {
            "route_count": current.get("route_count"),
            "routes_digest": current.get("routes_digest"),
            "mcp_count": current.get("mcp_catalog", {}).get("count"),
            "mcp_digest": current.get("mcp_catalog", {}).get("digest"),
            "import_cycles": len(current.get("import_cycles", [])),
            "openapi": current_openapi,
            "health": health,
            "cutover": cutover,
            "slo": slo,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--coverage-json", type=Path, required=True)
    parser.add_argument("--base-url", default=endpoint_url("bhm_api"))
    parser.add_argument("--latency-iterations", type=int, default=5)
    parser.add_argument("--coverage-floor", type=float, default=65.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    builder = load_baseline_builder()
    baseline = read_json(args.baseline)
    coverage = read_json(args.coverage_json)
    current = builder.build_baseline(args.base_url)
    startup = run_startup_probe()
    latency = run_latency(args.latency_iterations)
    report = compare_contracts(
        baseline,
        current,
        coverage,
        startup,
        latency,
        coverage_floor=args.coverage_floor,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
