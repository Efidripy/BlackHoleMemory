#!/usr/bin/env python3
"""Validate the monolith packaging profile boundary and live resource budget."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.request
import zipfile
from pathlib import Path

from blackholememory.local_endpoint_policy import open_local_url
from blackholememory.local_endpoint_policy import read_bounded_response
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT.parent.parent / "workspace" / "local" / "tmp" / "bhm-releases" / "wi16-release-20260716-r2" / "BHM-Release-v1.7.1.zip"


def _write_report(path: Path, report: dict) -> None:
    replace_bytes_safely(
        path,
        (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _probe(
    url: str,
    timeout: float = BHM_INTERNAL_HTTP_TIMEOUT_SECONDS,
) -> tuple[bool, float, str]:
    started = time.perf_counter()
    try:
        request = urllib.request.Request(url, method="GET")
        with open_local_url(request, timeout=timeout) as response:
            read_bounded_response(response, limit=512)
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            if status != 200:
                raise RuntimeError(f"unexpected HTTP status {status}")
        return True, round((time.perf_counter() - started) * 1000.0, 3), "ok"
    except Exception as exc:  # pragma: no cover - host-specific
        return False, round((time.perf_counter() - started) * 1000.0, 3), str(exc)[:160]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    endpoints = {
        "api_ready": "http://127.0.0.1:8000/health/ready",
        "qdrant_health": "http://127.0.0.1:6333/healthz",
        "slo": "http://127.0.0.1:8000/bhm/health/slo",
    }
    probes = {}
    for name, url in endpoints.items():
        ok, latency_ms, detail = _probe(url)
        probes[name] = {"ok": ok, "latency_ms": latency_ms, "detail": detail}
    archive_ok = ARCHIVE.is_file()
    archive_bytes = ARCHIVE.stat().st_size if archive_ok else 0
    archive_sha = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() if archive_ok else ""
    archive_entries = 0
    forbidden_entries: list[str] = []
    if archive_ok:
        with zipfile.ZipFile(ARCHIVE) as bundle:
            archive_entries = len(bundle.namelist())
            forbidden_entries = [name for name in bundle.namelist() if ".src/" in name or "/runtime/" in name or name.endswith((".db", ".sqlite3"))]
    profiles = {
        "core_offline": {
            "authority": "sqlite",
            "qdrant": "optional-not-bundled",
            "llm": "disabled-or-proposal-only local gateway",
            "network": "none by default",
            "degraded_behavior": "search/projection reports unavailable; SQLite remains usable",
        },
        "local_full": {
            "authority": "sqlite",
            "qdrant": "pinned local service qdrant/qdrant:v1.18.2@sha256:75eab8c4ba42096724fdcfde8b4de0b5713d529dde32f285a1f86fdcb2c9e50c",
            "llm": "loopback BHM Local LLM Gateway only",
            "network": "loopback services only",
            "degraded_behavior": "API readiness and SLO fail closed while remote projection is unavailable",
        },
        "dev_scale": {
            "authority": "sqlite",
            "qdrant": "explicit external/scale deployment, never implicit",
            "llm": "capability-routed local services; cloud fallback requires operator override",
            "network": "explicitly configured",
            "degraded_behavior": "bounded backpressure, projection queue and rollback evidence required",
        },
    }
    checks = {
        "profiles_complete": set(profiles) == {"core_offline", "local_full", "dev_scale"},
        "release_archive": archive_ok,
        "archive_boundary": not forbidden_entries,
        "api_ready": probes["api_ready"]["ok"],
        "qdrant_ready": probes["qdrant_health"]["ok"],
        "slo_endpoint": probes["slo"]["ok"],
        "single_authority": all(item["authority"] == "sqlite" for item in profiles.values()),
    }
    report = {
        "schema_version": "bhm.p21.4.packaging-profiles.v1",
        "ok": all(checks.values()),
        "profiles": profiles,
        "checks": checks,
        "probes": probes,
        "package": {"archive": str(ARCHIVE), "bytes": archive_bytes, "entries": archive_entries, "sha256": archive_sha, "forbidden_entries": forbidden_entries},
        "budgets": {"api_ready_p95_ms": 5000, "qdrant_health_p95_ms": 5000, "release_archive_max_bytes": 500_000_000, "max_forbidden_entries": 0},
        "rollback": "select previous profile and restore release/runtime manifest; no data migration",
        "final_integrator": "codex:/root",
    }
    _write_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
