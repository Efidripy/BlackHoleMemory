from __future__ import annotations

import io
import threading
import time

import pytest

import blackholememory.mcp_doctor as doctor
from blackholememory.local_endpoint_policy import LocalEndpointError
from blackholememory.mcp_doctor import choose_next_action
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES


def _state(**overrides):
    base = {
        "runtime": {
            "reachable": True,
            "ready": True,
            "cutover": True,
            "slo_ok": True,
            "projection_pending": 0,
            "outbox_pending": 0,
        },
        "configured": {"status": "aligned", "writes_live_state": False},
        "pipe": {"connected": True},
        "protocol": {"ok": True, "catalog": {"usable": True, "tool_count": len(CORE_TOOL_NAMES)}},
        "leases": {"status": "detached", "pending_count": 0},
        "ownership": {"status": "clean", "invalid_record_count": 0, "orphaned_count": 0},
        "duplicates": {"status": "clean"},
    }
    for key, value in overrides.items():
        base[key].update(value)
    return base


def test_doctor_next_action_prioritizes_runtime_slo_before_retained_duplicates():
    state = _state(
        runtime={"slo_ok": False, "projection_pending": 2},
        duplicates={"status": "retained_duplicates"},
    )

    action = choose_next_action(**state)

    assert action == {
        "severity": "high",
        "reason_code": "runtime_slo_breached",
        "action": "drain the authoritative projection outbox, then rerun MCP Doctor",
    }


def test_doctor_fails_closed_on_duplicate_truth():
    state = _state(duplicates={"status": "active_conflict"})

    action = choose_next_action(**state)

    assert action["severity"] == "high"
    assert action["reason_code"] == "active_duplicate_registration"


def test_doctor_fails_closed_on_active_duplicate_before_claiming_healthy():
    state = _state(duplicates={"status": "active_conflict"})

    action = choose_next_action(**state)

    assert action["severity"] == "high"
    assert action["reason_code"] == "active_duplicate_registration"


def test_doctor_catalog_gate_requires_exact_core_tool_count():
    state = _state(protocol={"ok": True, "catalog": {"usable": True, "tool_count": 11}})

    action = choose_next_action(**state)

    assert action["reason_code"] == "catalog_unusable"


def test_doctor_ownership_probe_is_never_promoted_to_broad_kill():
    state = _state(ownership={"status": "clean", "invalid_record_count": 0, "orphaned_count": 0})

    action = choose_next_action(**state)

    assert "kill" not in action["action"].casefold()


def test_doctor_config_rejects_external_base_url():
    with pytest.raises(LocalEndpointError, match="local-only"):
        doctor.DoctorConfig(base_url="https://api.example.com")


def test_doctor_health_headers_match_runtime_auth_boundary(monkeypatch):
    monkeypatch.setattr(doctor, "_required_bhm_caller_token", lambda: "t" * 43)

    anonymous = doctor._bhm_request_headers("/health/ready", accept="application/json")
    protected = doctor._bhm_request_headers("/health/cutover", accept="application/json")

    assert "Authorization" not in anonymous
    assert protected["Authorization"] == "Bearer " + ("t" * 43)


class _Response:
    status = 200

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


def test_doctor_get_json_uses_bounded_local_transport(monkeypatch):
    captured = {}

    def fake_open(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response(b'{"ok":true}')

    monkeypatch.setattr(doctor, "open_local_url", fake_open)

    payload, error = doctor._get_json(
        "http://127.0.0.1:8000",
        "/health/ready",
        timeout_seconds=20,
    )

    assert payload == {"ok": True}
    assert error is None
    assert captured == {
        "url": "http://127.0.0.1:8000/health/ready",
        "timeout": 15.0,
    }


def test_doctor_get_json_fails_closed_on_oversized_response(monkeypatch):
    monkeypatch.setattr(doctor, "open_local_url", lambda *_args, **_kwargs: _Response(b"x" * (doctor.MAX_HTTP_BYTES + 1)))

    payload, error = doctor._get_json(
        "http://127.0.0.1:8000",
        "/health/ready",
        timeout_seconds=2,
    )

    assert payload is None
    assert error == "response_too_large"


def test_doctor_mcp_request_fails_closed_on_oversized_response(monkeypatch):
    monkeypatch.setattr(doctor, "open_local_url", lambda *_args, **_kwargs: _Response(b"x" * (doctor.MAX_HTTP_BYTES + 1)))

    with pytest.raises(ValueError, match="response_too_large"):
        doctor._http_mcp_request(
            "http://127.0.0.1:8000/mcp",
            None,
            timeout_seconds=2,
            method="DELETE",
        )


def test_doctor_read_line_closes_blocked_stdout_after_deadline() -> None:
    released = threading.Event()

    class _BlockingStdout(io.BytesIO):
        def readline(self, *args, **kwargs):
            released.wait(timeout=1.0)
            return b'{"ok":true}\n'

        def close(self):
            released.set()
            super().close()

    class _Process:
        stdout = _BlockingStdout()

    process = _Process()
    started = time.perf_counter()
    with pytest.raises(TimeoutError, match="protocol_response_timeout"):
        doctor._read_line(process, timeout_seconds=0.01)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5
