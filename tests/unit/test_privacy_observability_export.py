from __future__ import annotations

import hashlib
import json

import pytest

from blackholememory.privacy_observability_export import MAX_INPUT_ROWS
from blackholememory.privacy_observability_export import ObservabilityExportConfig
from blackholememory.privacy_observability_export import ObservabilityExportError
from blackholememory.privacy_observability_export import build_observability_export_plan
from blackholememory.privacy_observability_export import build_prometheus_payload
from blackholememory.privacy_observability_export import verify_prometheus_payload


def _usage(operation: str = "POST_/bhm/search") -> dict:
    return {
        "operations": [
            {
                "surface": "rest",
                "operation": operation,
                "status_counts": {"2xx": 3, "5xx": 1},
                "latency_ms": {"p50": 4.0, "p95": 9.0},
            }
        ]
    }


def test_default_off_payload_is_content_free_and_deterministic():
    first = build_prometheus_payload(_usage("POST_/bhm/search?query=never-export"), outbox_counts={"pending": 2})
    second = build_prometheus_payload(_usage("POST_/bhm/search?query=never-export"), outbox_counts={"pending": 2})

    assert first == second
    assert verify_prometheus_payload(first)
    rendered = "\n".join(first["metrics"])
    assert 'route="other"' in rendered
    assert "never-export" not in rendered
    assert first["privacy"] == {"content": False, "identifiers": False, "query": False, "path": False, "errors": False}
    assert first["execution"] == {"network_performed": False, "persistent_mutation": False}
    assert build_observability_export_plan(ObservabilityExportConfig(), first) == {
        "schema_version": "bhm.privacy-observability-export.v1",
        "status": "disabled",
        "network_performed": False,
        "rollback": "retain-enabled-false",
    }


def test_payload_fails_closed_for_cardinality_and_tampering():
    with pytest.raises(ObservabilityExportError, match="bounded"):
        build_prometheus_payload({"operations": [_usage()["operations"][0]] * (MAX_INPUT_ROWS + 1)}, outbox_counts={})
    payload = build_prometheus_payload(_usage(), outbox_counts={"completed": 1})
    assert not verify_prometheus_payload({**payload, "metrics": []})
    with pytest.raises(ObservabilityExportError, match="digest"):
        build_observability_export_plan(ObservabilityExportConfig(), {**payload, "metrics": []})
    malicious_core = {
        "schema_version": payload["schema_version"],
        "format": payload["format"],
        "metrics": ['bhm_observability_requests_total{route="raw-secret"} 1'],
    }
    malicious = {
        **malicious_core,
        "payload_digest": hashlib.sha256(
            json.dumps(malicious_core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "privacy": payload["privacy"],
        "execution": payload["execution"],
    }
    assert not verify_prometheus_payload(malicious)


@pytest.mark.parametrize(
    ("endpoint", "allowed_hosts", "allow_redirects"),
    [
        ("http://metrics.example.test/push", ("metrics.example.test",), False),
        ("https://127.0.0.1/push", ("127.0.0.1",), False),
        ("https://metrics.example.test/push?token=forbidden", ("metrics.example.test",), False),
        ("https://metrics.example.test/push", ("other.example.test",), False),
        ("https://metrics.example.test/push", ("metrics.example.test",), True),
    ],
)
def test_egress_plan_requires_tls_allowlist_and_no_redirects(endpoint, allowed_hosts, allow_redirects):
    payload = build_prometheus_payload(_usage(), outbox_counts={})
    config = ObservabilityExportConfig(enabled=True, endpoint=endpoint, allowed_hosts=allowed_hosts, allow_redirects=allow_redirects)

    with pytest.raises(ObservabilityExportError):
        build_observability_export_plan(config, payload)


def test_valid_egress_plan_is_explicitly_non_executing_and_rollbackable():
    payload = build_prometheus_payload(_usage(), outbox_counts={"deferred": 2, "unknown": 1})
    plan = build_observability_export_plan(
        ObservabilityExportConfig(enabled=True, endpoint="https://metrics.example.test/push", allowed_hosts=("metrics.example.test",)),
        payload,
    )

    assert plan["status"] == "planned"
    assert plan["tls_required"] is True
    assert plan["redirects_allowed"] is False
    assert plan["network_performed"] is False
    assert plan["rollback"] == "set-enabled-false-and-discard-plan"
