from __future__ import annotations

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory.usage_telemetry import UsageTelemetry
from blackholememory.usage_telemetry import normalize_operation
from blackholememory.usage_telemetry import response_size_bucket


def test_normalize_operation_drops_queries_and_unbounded_identifiers():
    operation = normalize_operation(
        "GET /bhm/memory/123456789012345678901234567890?token=do-not-store"
    )

    assert operation == "GET_/bhm/memory/{id}"
    assert "do-not-store" not in operation


def test_response_size_bucket_is_coarse():
    assert response_size_bucket(None) == "unknown"
    assert response_size_bucket(0) == "0B"
    assert response_size_bucket(1024) == "1B-1KiB"
    assert response_size_bucket(1025) == "1KiB-10KiB"
    assert response_size_bucket(2 * 1024 * 1024) == ">1MiB"


def test_usage_snapshot_reports_rates_percentiles_and_surface():
    telemetry = UsageTelemetry(max_operations=8, max_latency_samples=8)
    telemetry.record(
        surface="rest",
        operation="GET /bhm/search",
        status="2xx",
        duration_ms=10,
        response_size_bytes=10,
    )
    telemetry.record(
        surface="rest",
        operation="GET /bhm/search",
        status="5xx",
        duration_ms=30,
        response_size_bytes=20_000,
    )
    telemetry.record(
        surface="mcp",
        operation="tools/call:bhm_search",
        status="timeout",
        duration_ms=100,
        timeout=True,
    )

    snapshot = telemetry.snapshot()
    assert snapshot["schema_version"] == 1
    assert snapshot["totals"] == {
        "count": 3,
        "errors": 2,
        "timeouts": 1,
        "error_rate": 0.666667,
        "timeout_rate": 0.333333,
    }
    assert snapshot["by_surface"]["rest"]["count"] == 2
    search = next(row for row in snapshot["operations"] if row["operation"] == "GET_/bhm/search")
    assert search["latency_ms"] == {"p50": 10.0, "p95": 30.0, "sample_count": 2}
    assert search["response_size_buckets"] == {
        "1B-1KiB": 1,
        "10KiB-100KiB": 1,
    }
    assert snapshot["privacy"] == {
        "raw_payloads": False,
        "raw_response_bodies": False,
        "query_strings": False,
        "headers": False,
        "full_identifiers": False,
    }


def test_usage_storage_is_bounded_and_resettable():
    telemetry = UsageTelemetry(max_operations=2, max_latency_samples=2)
    for index in range(10):
        telemetry.record(
            surface="rest",
            operation=f"GET /route/{index}",
            duration_ms=index,
        )

    snapshot = telemetry.snapshot()
    assert len(snapshot["operations"]) <= 2
    assert sum(row["count"] for row in snapshot["operations"]) == 10
    assert all(row["latency_ms"]["sample_count"] <= 2 for row in snapshot["operations"])

    telemetry.reset()
    assert telemetry.snapshot()["totals"]["count"] == 0


def test_rest_usage_endpoint_reports_route_template_without_query():
    bhm_app._USAGE_TELEMETRY.reset()
    client = TestClient(bhm_app.app)

    response = client.get("/bhm/projects?secret=never-store")
    report_response = client.get("/bhm/telemetry/usage")

    assert response.status_code == 200
    assert report_response.status_code == 200
    report = report_response.json()
    project_row = next(
        row for row in report["operations"] if row["operation"] == "GET_/bhm/projects"
    )
    assert project_row["surface"] == "rest"
    assert project_row["count"] == 1
    assert "never-store" not in report_response.text
