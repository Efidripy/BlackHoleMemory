from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from blackholememory import app as bhm_app


def test_embedding_warmup_probe_is_bounded_and_does_not_retain_vector(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            captured["read_limit"] = limit
            return b'{"data":[{"embedding":[0.1]}]}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(bhm_app, "open_local_url", fake_urlopen)
    monkeypatch.setattr(
        bhm_app,
        "settings",
        SimpleNamespace(
            mem0_embedding_model="text-embedding-nomic-embed-text-v1.5",
            mem0_openai_base_url="http://127.0.0.1:13666/v1/",
            mem0_api_key="",
        ),
    )

    bhm_app._post_provider_embedding_warmup_probe()

    assert captured["url"] == "http://127.0.0.1:13666/v1/embeddings"
    assert captured["payload"] == {
        "model": "text-embedding-nomic-embed-text-v1.5",
        "input": ["bhm semantic fusion warmup"],
        "encoding_format": "float",
    }
    assert captured["timeout"] == bhm_app._PROVIDER_EMBEDDING_WARMUP_TIMEOUT_SECONDS
    assert captured["read_limit"] == bhm_app._PROVIDER_EMBEDDING_WARMUP_MAX_RESPONSE_BYTES + 1


def test_qwen_provider_warmup_disables_thinking(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return b'{"choices":[{"message":{"role":"assistant","content":"pong"}}]}'

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(bhm_app, "open_local_url", fake_urlopen)
    monkeypatch.setattr(
        bhm_app,
        "settings",
        SimpleNamespace(
            mem0_llm_model="qwen2.5-coder-7b-instruct",
            mem0_openai_base_url="http://127.0.0.1:13666/v1",
            mem0_api_key="",
        ),
    )

    bhm_app._post_provider_warmup_probe()

    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["timeout"] == bhm_app._PROVIDER_WARMUP_TIMEOUT_SECONDS


def test_provider_warmup_accepts_normal_completion_larger_than_128_bytes(monkeypatch) -> None:
    body = json.dumps(
        {
            "id": "chatcmpl-warmup",
            "object": "chat.completion",
            "model": "qwen2.5-coder-7b-instruct",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}}],
            "padding": "x" * 512,
        }
    ).encode("utf-8")
    assert len(body) > 128

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit: int):
            return body[:limit]

    monkeypatch.setattr(bhm_app, "open_local_url", lambda *_args, **_kwargs: _Response())
    monkeypatch.setattr(
        bhm_app,
        "settings",
        SimpleNamespace(
            mem0_llm_model="qwen2.5-coder-7b-instruct",
            mem0_openai_base_url="http://127.0.0.1:13666/v1",
            mem0_api_key="",
        ),
    )

    bhm_app._post_provider_warmup_probe()


def test_provider_warmup_rejects_invalid_or_oversized_completion(monkeypatch) -> None:
    class _Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit: int):
            return self.body[:limit]

    monkeypatch.setattr(
        bhm_app,
        "settings",
        SimpleNamespace(
            mem0_llm_model="qwen2.5-coder-7b-instruct",
            mem0_openai_base_url="http://127.0.0.1:13666/v1",
            mem0_api_key="",
        ),
    )
    monkeypatch.setattr(bhm_app, "open_local_url", lambda *_args, **_kwargs: _Response(b'{"choices": []}'))
    with pytest.raises(OSError, match="completion choice"):
        bhm_app._post_provider_warmup_probe()

    oversized = b"x" * (bhm_app._PROVIDER_WARMUP_MAX_RESPONSE_BYTES + 1)
    monkeypatch.setattr(bhm_app, "open_local_url", lambda *_args, **_kwargs: _Response(oversized))
    with pytest.raises(bhm_app.urllib.error.URLError, match="bounded limit"):
        bhm_app._post_provider_warmup_probe()


def test_semantic_provider_warmup_runs_embedding_probe_only_when_enabled(monkeypatch) -> None:
    calls: list[str] = []

    async def run_probe() -> None:
        bhm_app._PROVIDER_WARMUP_READY.clear()
        monkeypatch.setattr(bhm_app, "_PROVIDER_WARMUP_REQUIRED", True)
        monkeypatch.setattr(bhm_app, "_PROVIDER_EMBEDDING_WARMUP_ENABLED", True)
        monkeypatch.setattr(bhm_app, "_PROVIDER_EMBEDDING_WARMUP_ATTEMPTS", 1)
        monkeypatch.setattr(bhm_app, "_post_provider_warmup_probe", lambda: calls.append("chat"))
        monkeypatch.setattr(
            bhm_app,
            "_post_provider_embedding_warmup_probe",
            lambda: calls.append("embedding"),
        )
        await bhm_app.warmup_provider_probe()

    asyncio.run(run_probe())

    assert calls == ["chat", "embedding"]
    status = bhm_app._get_provider_warmup_status()
    assert status["ready"] is True
    assert status["embedding_ready"] is True
    assert status["embedding_phase"] == "ready"
    bhm_app._PROVIDER_WARMUP_READY.clear()


def test_memory_warmup_only_instantiates_existing_collections(monkeypatch) -> None:
    calls: list[str] = []

    class _Client:
        def collection_exists(self, name: str) -> bool:
            return name.endswith("existing") or name == "global"

    class _Memory:
        embedding_model = object()

    monkeypatch.setenv("BHM_PROVIDER_WARMUP_PROJECTS", "existing,missing")
    monkeypatch.setattr(bhm_app, "_PROVIDER_MEMORY_WARMUP_MAX_PROJECTS", 8)
    monkeypatch.setattr(bhm_app, "get_qdrant_client", lambda: _Client())
    monkeypatch.setattr(bhm_app, "local_collection_name", lambda project: f"local_{project}")
    monkeypatch.setattr(bhm_app, "global_collection_name", lambda: "global")
    monkeypatch.setattr(
        bhm_app,
        "get_project_mem0_memory",
        lambda project: (calls.append(f"project:{project}") or _Memory()),
    )
    monkeypatch.setattr(
        bhm_app,
        "get_global_core_memory",
        lambda: (calls.append("global") or _Memory()),
    )

    warmed, skipped = bhm_app._post_provider_memory_warmup_probe()

    assert warmed == ["existing"]
    assert skipped == ["missing"]
    assert calls == ["project:existing", "global"]
