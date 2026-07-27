"""Deterministic MCP attach/catalog latency budget helpers."""

from __future__ import annotations

import math
from typing import Any

MCP_ATTACH_MAX_MS = 300.0
# P26 expands the canonical bounded catalog with code-intelligence tools. Keep
# a finite wire budget while allowing the versioned 23-tool schema to attach.
MCP_CATALOG_MAX_BYTES = 32_768


def percentile(values: list[float], quantile: float = 0.95) -> float:
    """Return a nearest-rank percentile without interpolating outliers away."""

    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0.0 < quantile <= 1.0:
        raise ValueError("quantile must be in (0, 1]")
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(len(ordered) * quantile))
    return round(ordered[rank - 1], 3)


def evaluate_mcp_latency(
    attach_samples_ms: list[float],
    catalog_bytes: int,
    *,
    max_attach_ms: float = MCP_ATTACH_MAX_MS,
    max_catalog_bytes: int = MCP_CATALOG_MAX_BYTES,
) -> dict[str, Any]:
    """Build the gate report used by the operator benchmark script."""

    attach_p95_ms = percentile(attach_samples_ms)
    checks = {
        "attach_p95_within_budget": attach_p95_ms <= max(float(max_attach_ms), 0.0),
        "catalog_within_budget": int(catalog_bytes) <= max(int(max_catalog_bytes), 0),
    }
    return {
        "ok": all(checks.values()),
        "attach_samples": len(attach_samples_ms),
        "attach_p95_ms": attach_p95_ms,
        "catalog_bytes": int(catalog_bytes),
        "budgets": {
            "attach_p95_ms": float(max_attach_ms),
            "catalog_bytes": int(max_catalog_bytes),
        },
        "checks": checks,
    }
