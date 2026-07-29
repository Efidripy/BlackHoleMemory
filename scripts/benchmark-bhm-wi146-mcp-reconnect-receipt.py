#!/usr/bin/env python
"""Bounded benchmark for the MCP reconnect/lease receipt."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from blackholememory.mcp_reconnect_receipt import build_mcp_reconnect_receipt  # noqa: E402


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def run(iterations: int = 1000) -> dict[str, object]:
    count = max(1, min(int(iterations), 5000))
    fixture = {
        "connected": {"state": "attached"},
        "catalog": {"state": "ready"},
        "runtime": {"state": "healthy"},
        "schema_drift": {"state": "none"},
        "rest_degraded": {"transport_ready": True},
        "http_sessions": {
            "expired_count": 0,
            "sessions": [
                {
                    "protocol_version": "2025-06-18",
                    "catalog_hash": "catalog-a",
                    "contract_digest": "contract-a",
                    "contract_state": "aligned",
                    "lease_remaining_seconds": 120,
                }
            ],
        },
    }
    latencies: list[float] = []
    first = ""
    deterministic = True
    for _ in range(count):
        started = time.perf_counter()
        receipt = build_mcp_reconnect_receipt(**fixture)
        latencies.append((time.perf_counter() - started) * 1000.0)
        stable = {key: value for key, value in receipt.items() if key != "generated_at"}
        digest = _digest(stable)
        if not first:
            first = digest
        deterministic = deterministic and digest == first
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "schema_version": "bhm.p28.wi146.mcp-reconnect-receipt-benchmark.v1",
        "iterations": count,
        "p50_ms": round(ordered[len(ordered) // 2], 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(ordered), 3),
        "stable_digest": first,
        "deterministic": deterministic,
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "network": False,
            "client_reconnect": False,
            "secrets_returned": False,
        },
        "ok": deterministic and p95 <= 20.0,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
