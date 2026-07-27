from __future__ import annotations

import pytest

from blackholememory.mcp_latency import evaluate_mcp_latency
from blackholememory.mcp_latency import percentile


def test_percentile_uses_nearest_rank_and_preserves_tail():
    assert percentile([1.0, 2.0, 3.0, 100.0]) == 100.0


def test_mcp_latency_gate_accepts_core_budget():
    report = evaluate_mcp_latency([2.0, 3.0, 4.0], 12_284)

    assert report["ok"] is True
    assert report["checks"] == {
        "attach_p95_within_budget": True,
        "catalog_within_budget": True,
    }


@pytest.mark.parametrize(
    ("samples", "catalog_bytes"),
    [([1.0, 301.0], 12_284), ([1.0, 2.0], 32_769)],
)
def test_mcp_latency_gate_fails_closed_for_budget_breach(samples, catalog_bytes):
    report = evaluate_mcp_latency(samples, catalog_bytes)

    assert report["ok"] is False
