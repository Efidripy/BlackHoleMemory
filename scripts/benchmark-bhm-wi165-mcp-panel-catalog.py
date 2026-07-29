"""Deterministic benchmark for WI-165 MCP panel catalog coverage."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blackholememory.mcp_panel import build_mcp_panel_snapshot


def main() -> int:
    fixture = {
        "configured": {"status": "configured"},
        "attach": {"status": "detached", "leases": []},
        "connection": {},
        "telemetry": {},
        "runtime": {"ready": {"ok": True}, "cutover": {"ok": True}, "slo": {"ok": True, "status": "healthy"}},
        "http_sessions": {
            "status": "attached",
            "attached_count": 1,
            "pending_count": 0,
            "sessions": [{
                "state": "catalog_ready",
                "client_version": "1.7.1",
                "catalog_hash": "catalog-wi165-35",
                "contract_digest": "contract-wi165",
                "contract_state": "verified",
                "tool_count": 35,
                "lease_remaining_seconds": 120,
            }],
        },
    }
    digest = hashlib.sha256(json.dumps(fixture, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    samples: list[float] = []
    result = None
    for _ in range(1000):
        started = time.perf_counter()
        result = build_mcp_panel_snapshot(**fixture)
        samples.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(samples)
    p50 = round(ordered[len(ordered) // 2], 3)
    p95 = round(ordered[int(round((len(ordered) - 1) * 0.95))], 3)
    payload = {
        "schema_version": "bhm.p28.wi165.mcp-panel-benchmark.v1",
        "fixture_digest": digest,
        "iterations": len(samples),
        "p50_ms": p50,
        "p95_ms": p95,
        "max_ms": round(max(samples), 3),
        "deterministic": result["catalog_coverage"]["state"] == "pass" and result["catalog"]["observed_tool_count"] == 35,
        "catalog_coverage": result["catalog_coverage"],
        "execution": {"read_only": True, "writes_live_state": False, "native_attach_claim": False},
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
