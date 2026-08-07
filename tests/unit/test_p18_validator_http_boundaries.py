from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from blackholememory.local_endpoint_policy import LocalEndpointError
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DOCTOR = _load("validate_bhm_p18_14_mcp_doctor", "validate-bhm-p18.14-mcp-doctor.py")
PANEL = _load("validate_bhm_p18_15_mcp_panel", "validate-bhm-p18.15-mcp-panel.py")
REPAIR = _load("validate_bhm_p18_16_mcp_repair", "validate-bhm-p18.16-mcp-repair.py")


class _Response:
    status = 200

    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        assert limit == 128 * 1024 + 1
        return self.payload


def test_doctor_get_json_uses_local_bounded_transport(monkeypatch) -> None:
    calls: list[str] = []

    def fake_open(request, *, timeout):
        calls.append(request.full_url)
        assert timeout == BHM_INTERNAL_HTTP_TIMEOUT_SECONDS
        return _Response(b'{"status":"healthy"}')

    monkeypatch.setattr(DOCTOR, "open_local_url", fake_open)
    assert DOCTOR._get_json("http://127.0.0.1:8000", "/bhm/health/slo") == {"status": "healthy"}
    assert calls == ["http://127.0.0.1:8000/bhm/health/slo"]


def test_doctor_get_json_fails_closed_on_non_200(monkeypatch) -> None:
    class NotReady(_Response):
        status = 503

    monkeypatch.setattr(DOCTOR, "open_local_url", lambda *_args, **_kwargs: NotReady(b"{}"))
    try:
        DOCTOR._get_json("http://127.0.0.1:8000", "/bhm/health/slo")
    except RuntimeError as exc:
        assert "unexpected HTTP status 503" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("non-200 response must fail closed")


def test_panel_get_json_uses_local_bounded_transport(monkeypatch) -> None:
    monkeypatch.setattr(PANEL, "configured_caller_token", lambda: "t" * 40)
    calls: list[str] = []

    def fake_open(request, *, timeout):
        calls.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer " + ("t" * 40)
        assert timeout == BHM_INTERNAL_HTTP_TIMEOUT_SECONDS
        return _Response(b'{"state":"healthy"}')

    monkeypatch.setattr(PANEL, "open_local_url", fake_open)
    assert PANEL._get_json("http://127.0.0.1:8000", "/bhm/telemetry/mcp-panel") == {"state": "healthy"}
    assert calls == ["http://127.0.0.1:8000/bhm/telemetry/mcp-panel"]


def test_panel_get_json_rejects_missing_caller_token(monkeypatch) -> None:
    monkeypatch.setattr(PANEL, "configured_caller_token", lambda: "short")
    try:
        PANEL._get_json("http://127.0.0.1:8000", "/bhm/telemetry/mcp-panel")
    except RuntimeError as exc:
        assert "unavailable" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("short caller token must fail closed")


def test_repair_json_request_uses_local_bounded_transport(monkeypatch) -> None:
    monkeypatch.setattr(REPAIR, "configured_caller_token", lambda: "t" * 40)
    calls: list[str] = []

    class RepairResponse(_Response):
        def read(self, limit: int) -> bytes:
            assert limit == 256 * 1024 + 1
            return self.payload

    def fake_open(request, *, timeout):
        calls.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer " + ("t" * 40)
        assert timeout == BHM_INTERNAL_HTTP_TIMEOUT_SECONDS
        return RepairResponse(b'{"status":"healthy"}')

    monkeypatch.setattr(REPAIR, "open_local_url", fake_open)
    assert REPAIR._json_request(
        "http://127.0.0.1:8000/bhm/health/slo", caller_auth=True
    ) == (200, {"status": "healthy"})
    assert calls == ["http://127.0.0.1:8000/bhm/health/slo"]


def test_p18_validator_http_call_sites_are_registry_backed() -> None:
    for module in (DOCTOR, PANEL, REPAIR):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "BHM_INTERNAL_HTTP_TIMEOUT_SECONDS" in source
        assert "timeout=15" not in source


def test_repair_json_request_rejects_non_local_endpoint() -> None:
    with pytest.raises(LocalEndpointError, match="local-only"):
        REPAIR._json_request("https://example.com/bhm/health/slo")
