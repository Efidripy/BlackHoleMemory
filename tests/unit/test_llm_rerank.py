from __future__ import annotations

import asyncio

import httpx

from blackholememory import llm_rerank


def _hits() -> list[dict]:
    return [
        {"id": "first", "content": "alpha", "metadata": {}},
        {"id": "second", "content": "beta", "metadata": {}},
    ]


def _confident_hits(*, first_score: float = 0.9, second_score: float = 0.8, first_sem: int = 1, first_lex: int = 1) -> list[dict]:
    return [
        {
            "id": "first",
            "content": "alpha",
            "score": first_score,
            "metadata": {"semantic_rank": first_sem, "lexical_rank": first_lex},
        },
        {
            "id": "second",
            "content": "beta",
            "score": second_score,
            "metadata": {"semantic_rank": 2, "lexical_rank": 2},
        },
    ]


class _FakeClient:
    calls = 0
    mode = "success"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url, *, json, timeout):
        type(self).calls += 1
        assert timeout == 12.0
        assert json["chat_template_kwargs"] == {"enable_thinking": False}
        if self.mode == "error":
            raise httpx.ConnectError("fake server")
        if self.mode == "timeout":
            raise TimeoutError("fake timeout")
        score = 9 if "beta" in json["messages"][1]["content"] else 2
        return _FakeResponse({"choices": [{"message": {"content": '{"score": %s}' % score}}]})


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _configure(monkeypatch, *, enabled=True):
    values = {
        "BHM_LLM_RERANK": "1" if enabled else "0",
        "BHM_LLM_RERANK_BASE_URL": "http://rerank.test/v1",
        "BHM_LLM_RERANK_MODEL": "test-model",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    llm_rerank.clear_rerank_cache()
    _FakeClient.calls = 0
    _FakeClient.mode = "success"
    monkeypatch.setattr(llm_rerank.httpx, "AsyncClient", _FakeClient)


def test_disabled_does_not_call_server(monkeypatch):
    _configure(monkeypatch, enabled=False)
    hits = _hits()
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert result is hits
    assert _FakeClient.calls == 0


def test_enabled_reranks_and_records_scores(monkeypatch):
    _configure(monkeypatch)
    result = asyncio.run(llm_rerank.rerank_hits("question", _hits()))
    assert [hit["id"] for hit in result] == ["second", "first"]
    assert result[0]["metadata"]["llm_rerank_score"] == 9.0
    assert result[1]["metadata"]["llm_rerank_score"] == 2.0
    assert _FakeClient.calls == 2


def test_server_error_keeps_original_order(monkeypatch):
    _configure(monkeypatch)
    _FakeClient.mode = "error"
    hits = _hits()
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert [hit["id"] for hit in result] == ["first", "second"]
    assert all("llm_rerank_score" not in hit["metadata"] for hit in result)


def test_timeout_keeps_original_order(monkeypatch):
    _configure(monkeypatch)
    _FakeClient.mode = "timeout"
    hits = _hits()
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert [hit["id"] for hit in result] == ["first", "second"]


def test_scores_are_cached(monkeypatch):
    _configure(monkeypatch)
    hits = _hits()
    asyncio.run(llm_rerank.rerank_hits("question", hits))
    first_call_count = _FakeClient.calls
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == first_call_count
    assert [hit["id"] for hit in result] == ["second", "first"]


def test_blend_lets_hybrid_rank_break_close_model_scores(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0.3")
    result = asyncio.run(llm_rerank.rerank_hits("question", _hits()))
    assert [hit["id"] for hit in result] == ["second", "first"]
    llm_rerank.clear_rerank_cache()
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "1")
    result = asyncio.run(llm_rerank.rerank_hits("question", _hits()))
    assert [hit["id"] for hit in result] == ["first", "second"]
    assert result[0]["metadata"]["llm_rerank_score"] == 2.0


def test_default_does_not_skip_confident_head(monkeypatch):
    _configure(monkeypatch)
    hits = _confident_hits()
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 2
    assert [hit["id"] for hit in result] == ["second", "first"]
    assert result[0]["metadata"]["llm_rerank_score"] == 9.0


def test_confident_head_skips_model_calls_and_preserves_hits(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_SKIP_GAP", "0.05")
    hits = _confident_hits(first_score=0.85, second_score=0.80)
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 0
    assert result is hits
    assert [hit["id"] for hit in result] == ["first", "second"]
    assert all("llm_rerank_score" not in hit["metadata"] for hit in result)


def test_unconfident_head_small_gap_calls_model(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_SKIP_GAP", "0.05")
    hits = _confident_hits(first_score=0.84, second_score=0.80)
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 2
    assert [hit["id"] for hit in result] == ["second", "first"]
    assert result[0]["metadata"]["llm_rerank_score"] == 9.0


def test_unconfident_head_ranks_not_top_calls_model(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_SKIP_GAP", "0.05")
    # semantic_rank not 1
    hits = _confident_hits(first_score=0.9, second_score=0.8, first_sem=2, first_lex=1)
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 2
    assert [hit["id"] for hit in result] == ["second", "first"]

    _FakeClient.calls = 0
    llm_rerank.clear_rerank_cache()
    # lexical_rank not 1
    hits = _confident_hits(first_score=0.9, second_score=0.8, first_sem=1, first_lex=2)
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 2
    assert [hit["id"] for hit in result] == ["second", "first"]

    _FakeClient.calls = 0
    llm_rerank.clear_rerank_cache()
    # ranks missing
    hits = [
        {"id": "first", "content": "alpha", "score": 0.9, "metadata": {}},
        {"id": "second", "content": "beta", "score": 0.8, "metadata": {}},
    ]
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 2
    assert [hit["id"] for hit in result] == ["second", "first"]


def test_confident_head_requires_at_least_two_candidates(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_SKIP_GAP", "0.05")
    hits = [
        {"id": "first", "content": "alpha", "score": 0.9, "metadata": {"semantic_rank": 1, "lexical_rank": 1}},
    ]
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 1
    assert result[0]["metadata"]["llm_rerank_score"] == 2.0


def test_garbage_skip_gap_env_disables_skip(monkeypatch):
    _configure(monkeypatch)
    for garbage in ("invalid-text", "-0.05", "-1", "0", "nan", "inf", ""):
        monkeypatch.setenv("BHM_LLM_RERANK_SKIP_GAP", garbage)
        _FakeClient.calls = 0
        llm_rerank.clear_rerank_cache()
        hits = _confident_hits(first_score=0.9, second_score=0.8)
        result = asyncio.run(llm_rerank.rerank_hits("question", hits))
        assert _FakeClient.calls == 2, f"Failed for garbage value: {garbage!r}"
        assert [hit["id"] for hit in result] == ["second", "first"]


def test_confident_head_helper_direct(monkeypatch):
    monkeypatch.setenv("BHM_LLM_RERANK_SKIP_GAP", "0.05")
    hits = _confident_hits(first_score=0.9, second_score=0.8)
    assert llm_rerank._confident_head(hits)
    assert not llm_rerank._confident_head([])
    assert not llm_rerank._confident_head(hits[:1])
    assert not llm_rerank._confident_head([{"invalid": "hit"}, {"invalid": "second"}])
    no_score = [
        {"id": "first", "metadata": {"semantic_rank": 1, "lexical_rank": 1}},
        {"id": "second", "score": 0.5, "metadata": {}},
    ]
    assert not llm_rerank._confident_head(no_score)
