from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

from blackholememory.local_endpoint_policy import LocalEndpointError


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "validate_bhm_p21_0_streamable_http",
    ROOT / "scripts" / "validate-bhm-streamable-http-contract.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_bounded_json_rejects_oversized_response() -> None:
    response = httpx.Response(200, headers={"content-length": "999999999"}, content=b"{}")
    with pytest.raises(ValueError, match="bounded limit"):
        MODULE._bounded_json(response)


def test_probe_uses_local_only_proxy_free_no_redirect_transport(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_TOKEN", "x" * 32)
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            return None

    monkeypatch.setattr(MODULE.httpx, "Client", FakeClient)
    probe = MODULE.Probe("http://127.0.0.1:8000", 30.0)
    try:
        assert probe.base_url == "http://127.0.0.1:8000"
        assert captured == {"timeout": 30.0, "follow_redirects": False, "trust_env": False}
    finally:
        probe.close()


def test_probe_rejects_non_local_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("BHM_CALLER_TOKEN", "x" * 32)
    with pytest.raises(LocalEndpointError, match="local-only"):
        MODULE.Probe("https://example.com", 30.0)
