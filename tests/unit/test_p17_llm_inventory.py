from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-p17.1-llm-inventory.py"


def load_inventory():
    spec = importlib.util.spec_from_file_location("bhm_p17_llm_inventory", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_process_discovery_is_local_and_redacts_api_key():
    inventory = load_inventory()
    result = inventory.discover_llama_process(
        [
            {
                "pid": 42,
                "name": "llama-server.exe",
                "cmdline": "llama-server.exe --model C:\\models\\qwen.gguf --host 127.0.0.1 --port 57718 --ctx-size 8192 --parallel 4 --api-key secret",
            }
        ]
    )
    assert result["host"] == "127.0.0.1"
    assert result["port"] == 57718
    assert result["loaded_context"] == 8192
    assert result["parallel"] == 4
    assert result["api_key_sha256"]
    assert "secret" not in str(result)


def test_public_host_is_not_local_only():
    inventory = load_inventory()
    assert inventory._is_local_host("8.8.8.8") is False
    assert inventory._is_local_host("172.18.0.1") is True


def test_llm_inventory_timeout_is_registry_bounded():
    inventory = load_inventory()
    assert inventory.DEFAULT_TIMEOUT == 20.0
    assert inventory.bounded_llm_inventory_timeout(3) == 3.0
    assert inventory.bounded_llm_inventory_timeout(999) == 20.0
    assert inventory.bounded_llm_inventory_timeout(0) == 0.1
    with pytest.raises(ValueError, match="finite"):
        inventory.bounded_llm_inventory_timeout(float("inf"))


def test_hardware_probe_uses_registry_process_bound(monkeypatch):
    inventory = load_inventory()
    captured: dict[str, object] = {}

    def fake_run(*_args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return SimpleNamespace(returncode=1, stdout="", stderr="unavailable")

    monkeypatch.setattr(inventory.subprocess, "run", fake_run)
    assert inventory.hardware_snapshot() == {"available": False, "error": "unavailable"}
    assert captured["timeout"] == inventory.PROCESS_EXECUTION_LLM_INVENTORY_HARDWARE_TIMEOUT_SECONDS


class _Response:
    status = 200

    def __init__(self, body: bytes):
        self.body = body
        self.limits: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        self.limits.append(limit)
        return self.body if len(self.body) <= limit else self.body[:limit]


def test_request_json_uses_bounded_local_transport(monkeypatch):
    inventory = load_inventory()
    response = _Response(json.dumps({"data": []}).encode())
    captured: dict[str, object] = {}

    def fake_open(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return response

    monkeypatch.setattr(inventory, "open_local_url", fake_open)
    ok, payload, error = inventory._request_json(
        "http://127.0.0.1:57718/v1/models",
        method="GET",
        headers={},
        timeout=2.0,
    )

    assert ok is True
    assert payload == {"data": []}
    assert error == ""
    assert captured == {"url": "http://127.0.0.1:57718/v1/models", "timeout": 2.0}
    assert response.limits == [inventory.MAX_HTTP_RESPONSE_BYTES + 1]


def test_request_json_rejects_external_endpoint_before_open(monkeypatch):
    inventory = load_inventory()
    called = False

    def fail_open(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("external endpoint must not be opened")

    monkeypatch.setattr(inventory, "open_local_url", fail_open)
    ok, payload, error = inventory._request_json(
        "https://example.com/v1/models",
        method="GET",
        headers={},
        timeout=1.0,
    )
    assert ok is False
    assert payload == {}
    assert "local-only" in error
    assert called is False


def test_request_json_fails_closed_on_oversized_response(monkeypatch):
    inventory = load_inventory()
    response = _Response(b"x" * (inventory.MAX_HTTP_RESPONSE_BYTES + 1))
    monkeypatch.setattr(inventory, "open_local_url", lambda *_args, **_kwargs: response)
    ok, payload, error = inventory._request_json(
        "http://127.0.0.1:57718/v1/models",
        method="GET",
        headers={},
        timeout=1.0,
    )
    assert ok is False
    assert payload == {}
    assert "bounded limit" in error
