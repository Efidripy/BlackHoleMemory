from __future__ import annotations

import pytest

import blackholememory.local_llm_dualstack as dualstack


def test_dualstack_endpoint_urls_are_exact_loopback_literals() -> None:
    assert dualstack.endpoint_url("127.0.0.1", 13666) == "http://127.0.0.1:13666/v1/models"
    assert dualstack.endpoint_url("::1", 13666) == "http://[::1]:13666/v1/models"
    with pytest.raises(ValueError, match="exact loopback"):
        dualstack.endpoint_url("192.168.1.9", 13666)


def test_dualstack_report_distinguishes_ipv4_only_without_starting_a_model(monkeypatch) -> None:
    def fake_probe(host: str, port: int, *, timeout_seconds: float) -> dict:
        return {
            "host": host,
            "status": "ready" if host == "127.0.0.1" else "connection_refused",
            "http_status": 200 if host == "127.0.0.1" else None,
            "model_count": 2 if host == "127.0.0.1" else 0,
            "latency_ms": 1.0,
        }

    monkeypatch.setattr(dualstack, "probe_family", fake_probe)
    report = dualstack.dualstack_report(13666)

    assert report["ok"] is True
    assert report["readiness"] == "ipv4_only"
    assert report["probes"]["ipv6"]["status"] == "connection_refused"
    assert report["execution"]["model_started"] is False
    assert report["execution"]["sqlite_mutation"] is False


def test_dualstack_report_marks_both_families_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        dualstack,
        "probe_family",
        lambda host, port, *, timeout_seconds: {
            "host": host,
            "status": "timeout",
            "http_status": None,
            "model_count": 0,
            "latency_ms": 2.0,
        },
    )
    report = dualstack.dualstack_report(13666)

    assert report["ok"] is False
    assert report["readiness"] == "unavailable"
