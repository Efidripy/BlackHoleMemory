from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-native-retrieval-acceptance.py"
SPEC = importlib.util.spec_from_file_location("bhm_native_retrieval_acceptance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_native_acceptance_contract_is_read_only_and_bounded():
    text = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "native-mem0-qdrant",
        "user_id",
        '"data"',
        "mutation",
        "memory.search",
        "compatibility fallback is not called",
        r"172\.18",
        "endpoint_url(\"lm_studio\")",
    ):
        assert marker in text


def test_native_acceptance_has_no_mutating_qdrant_calls():
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("set_payload", "overwrite_payload", "upsert", "delete"):
        assert forbidden not in text


def test_native_acceptance_provider_probe_uses_registry_timeout():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "LLM_INVENTORY_HTTP_TIMEOUT_SECONDS" in text
    assert "urlopen(request, timeout=2)" not in text
    assert "urlopen(" not in text
    assert "open_local_url(" in text
    assert "read_bounded_response(" in text


def test_native_acceptance_provider_probe_uses_local_transport_and_bounded_body(monkeypatch):
    calls: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def read(self, limit):
            calls["read_limit"] = limit
            return b"ok"

    def open_response(request, *, timeout, endpoint):
        calls["url"] = request.full_url
        calls["timeout"] = timeout
        calls["endpoint"] = endpoint
        return Response()

    monkeypatch.setattr(MODULE, "open_local_url", open_response)

    assert MODULE._provider_is_live("http://127.0.0.1:1234/v1") is True
    assert calls == {
        "url": "http://127.0.0.1:1234/v1/models",
        "timeout": MODULE.LLM_INVENTORY_HTTP_TIMEOUT_SECONDS,
        "endpoint": "http://127.0.0.1:1234/v1",
        "read_limit": 129,
    }


class _OversizedResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def read(self, limit):
        return b"x" * limit


@pytest.mark.parametrize(
    "base_url, opener",
    [
        ("http://example.com/v1", None),
        ("http://127.0.0.1:1234/v1", lambda *_args, **_kwargs: _OversizedResponse()),
    ],
)
def test_native_acceptance_provider_probe_fails_closed_for_invalid_or_oversized_response(
    monkeypatch,
    base_url,
    opener,
):
    if opener is not None:
        monkeypatch.setattr(MODULE, "open_local_url", opener)

    assert MODULE._provider_is_live(base_url) is False
