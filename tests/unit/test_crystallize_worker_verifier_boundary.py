from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

from blackholememory.local_endpoint_policy import LocalEndpointError
from blackholememory.local_endpoint_policy import MAX_RESPONSE_BYTES
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify-bhm-crystallize-worker.py"
SPEC = importlib.util.spec_from_file_location("verify_bhm_crystallize_worker", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_verifier_client_is_local_only_and_registry_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(MODULE, "_required_bhm_caller_token", lambda: "t" * 32)
    monkeypatch.setattr(MODULE.httpx, "Client", FakeClient)

    MODULE._bhm_client("http://127.0.0.1:8000")

    assert captured["base_url"] == "http://127.0.0.1:8000"
    assert captured["timeout"] == float(BHM_INTERNAL_HTTP_TIMEOUT_SECONDS)
    assert captured["follow_redirects"] is False
    assert captured["trust_env"] is False


def test_verifier_rejects_remote_endpoint_before_authenticated_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MODULE, "_required_bhm_caller_token", lambda: "t" * 32)

    with pytest.raises(LocalEndpointError, match="loopback/private"):
        MODULE._bhm_client("https://example.com")


def test_verifier_bounds_json_response_before_parsing() -> None:
    response = httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    with pytest.raises(ValueError, match="bounded limit"):
        MODULE._bounded_json_response(response)
