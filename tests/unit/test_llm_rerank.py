from __future__ import annotations

import asyncio
import json

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
    response_content = None
    last_json = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url, *, json, timeout):
        type(self).calls += 1
        type(self).last_json = json
        assert timeout == 12.0
        assert json["chat_template_kwargs"] == {"enable_thinking": False}
        if self.mode == "error":
            raise httpx.ConnectError("fake server")
        if self.mode == "timeout":
            raise TimeoutError("fake timeout")
        if self.response_content is not None:
            return _FakeResponse({"choices": [{"message": {"content": self.response_content}}]})
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
        "BHM_LLM_RERANK_MODE": "pointwise",  # default is listwise3; these tests cover pointwise
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    llm_rerank.clear_rerank_cache()
    _FakeClient.calls = 0
    _FakeClient.mode = "success"
    _FakeClient.response_content = None
    _FakeClient.last_json = None
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


def test_listwise_reranks_with_one_request_and_scores_non_answers(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise")
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0")
    _FakeClient.response_content = '{"order":[2],"answers":[2]}'
    result = asyncio.run(llm_rerank.rerank_hits("question", _hits()))
    assert _FakeClient.calls == 1
    assert [hit["id"] for hit in result] == ["second", "first"]
    assert result[0]["metadata"]["llm_rerank_score"] == 10.0
    assert result[1]["metadata"]["llm_rerank_score"] == 2.0


def test_listwise_without_answers_caps_scores(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise")
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0")
    _FakeClient.response_content = '{"order":[2,1],"answers":[]}'
    result = asyncio.run(llm_rerank.rerank_hits("question", _hits()))
    assert [hit["metadata"]["llm_rerank_score"] for hit in result] == [7.0, 7.0]


def test_listwise_invalid_response_keeps_original_hits(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise")
    _FakeClient.response_content = "not json"
    hits = _hits()
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert [hit["id"] for hit in result] == ["first", "second"]
    assert all("llm_rerank_score" not in hit["metadata"] for hit in result)


def test_listwise_discards_duplicate_and_unknown_numbers(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise")
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0")
    _FakeClient.response_content = '{"order":[9,2,2,1],"answers":[2,99,2]}'
    result = asyncio.run(llm_rerank.rerank_hits("question", _hits()))
    assert _FakeClient.calls == 1
    assert [hit["id"] for hit in result] == ["second", "first"]
    assert result[0]["metadata"]["llm_rerank_score"] == 10.0


def test_listwise_server_error_keeps_original_hits(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise")
    _FakeClient.mode = "error"
    hits = _hits()
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 1
    assert [hit["id"] for hit in result] == ["first", "second"]
    assert all("llm_rerank_score" not in hit["metadata"] for hit in result)


def test_unknown_mode_uses_pointwise(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "unexpected")
    result = asyncio.run(llm_rerank.rerank_hits("question", _hits()))
    assert _FakeClient.calls == 2
    assert [hit["id"] for hit in result] == ["second", "first"]


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


def test_listwise3_reranks_with_one_request_and_translates_buckets(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise3")
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0")
    _FakeClient.response_content = (
        '{"analysis": "c2 is direct, c4 is context, c1 is irrelevant", '
        '"direct_answers": [2], "useful_context": [4], "irrelevant": [1]}'
    )
    hits = [
        {"id": "c1", "content": "first", "metadata": {}},
        {"id": "c2", "content": "second", "metadata": {}},
        {"id": "c3", "content": "third", "metadata": {}},
        {"id": "c4", "content": "fourth", "metadata": {}},
    ]
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 1
    assert [hit["id"] for hit in result] == ["c2", "c4", "c3", "c1"]
    scores = {hit["id"]: hit["metadata"]["llm_rerank_score"] for hit in result}
    assert scores["c2"] == 10.0
    assert scores["c4"] == 4.6
    assert scores["c3"] == 4.0
    assert scores["c1"] == 1.5


def test_listwise3_direct_answers_decay_with_floor(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise3")
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0")
    monkeypatch.setenv("BHM_LLM_RERANK_TOP_N", "20")
    order = list(range(1, 18))
    _FakeClient.response_content = json.dumps({
        "analysis": "all direct",
        "direct_answers": order,
        "useful_context": [],
        "irrelevant": [],
    })
    hits = [{"id": f"c{i}", "content": f"doc{i}", "metadata": {}} for i in order]
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 1
    scores = [hit["metadata"]["llm_rerank_score"] for hit in result]
    assert round(scores[0], 2) == 10.0
    assert round(scores[1], 2) == 9.65
    assert round(scores[2], 2) == 9.30
    assert round(scores[14], 2) == 5.10
    assert round(scores[15], 2) == 5.0
    assert round(scores[16], 2) == 5.0


def test_listwise3_single_direct_answer_scores_ten(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise3")
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0")
    _FakeClient.response_content = '{"analysis": "exact match", "direct_answers": [1], "useful_context": [], "irrelevant": []}'
    result = asyncio.run(llm_rerank.rerank_hits("question", _hits()))
    assert _FakeClient.calls == 1
    assert result[0]["id"] == "first"
    assert result[0]["metadata"]["llm_rerank_score"] == 10.0
    assert result[1]["id"] == "second"
    assert result[1]["metadata"]["llm_rerank_score"] == 4.0


def test_listwise3_parses_string_ids_and_markdown_garbage(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise3")
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0")
    _FakeClient.response_content = (
        'Here is my verdict:\n```json\n'
        '{\n'
        '  "analysis": "candidate 2 is exact, candidate 1 is useful",\n'
        '  "direct_answers": ["2"],\n'
        '  "useful_context": [" 1 "],\n'
        '  "irrelevant": []\n'
        '}\n'
        '```\nDone.'
    )
    result = asyncio.run(llm_rerank.rerank_hits("question", _hits()))
    assert _FakeClient.calls == 1
    assert [hit["id"] for hit in result] == ["second", "first"]
    assert result[0]["metadata"]["llm_rerank_score"] == 10.0
    assert result[1]["metadata"]["llm_rerank_score"] == 4.6


def test_listwise3_discards_out_of_range_and_duplicate_ids_with_precedence(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise3")
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0")
    _FakeClient.response_content = json.dumps({
        "analysis": "mixed duplicate data",
        "direct_answers": [2, "2", 99, 0],
        "useful_context": [2, 1],
        "irrelevant": [1, 2, "abc"],
    })
    result = asyncio.run(llm_rerank.rerank_hits("question", _hits()))
    assert _FakeClient.calls == 1
    assert [hit["id"] for hit in result] == ["second", "first"]
    assert result[0]["metadata"]["llm_rerank_score"] == 10.0
    assert result[1]["metadata"]["llm_rerank_score"] == 4.6


def test_listwise3_empty_lists_and_invalid_json_keeps_original_hits(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise3")

    for invalid in (
        '{"analysis": "no matches", "direct_answers": [], "useful_context": [], "irrelevant": []}',
        '{"analysis": "all out of range", "direct_answers": [99], "useful_context": [0], "irrelevant": [-5]}',
        "plain text not json at all",
        '{"broken json: true',
        "[]",
    ):
        _FakeClient.calls = 0
        _FakeClient.response_content = invalid
        hits = _hits()
        result = asyncio.run(llm_rerank.rerank_hits("question", hits))
        assert [hit["id"] for hit in result] == ["first", "second"], f"Failed for: {invalid}"
        assert all("llm_rerank_score" not in hit["metadata"] for hit in result)


def test_listwise3_server_error_and_timeout_keeps_original_hits(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise3")

    _FakeClient.mode = "error"
    hits = _hits()
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert _FakeClient.calls == 1
    assert [hit["id"] for hit in result] == ["first", "second"]
    assert all("llm_rerank_score" not in hit["metadata"] for hit in result)

    _FakeClient.calls = 0
    _FakeClient.mode = "timeout"
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert [hit["id"] for hit in result] == ["first", "second"]
    assert all("llm_rerank_score" not in hit["metadata"] for hit in result)


def test_listwise3_prompt_formatting_and_whitespace_collapse(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise3")
    hits = [
        {"id": "first", "title": "Doc 1", "content": "  alpha   \n\n  beta \t gamma  ", "metadata": {}},
        {"id": "second", "title": "Doc 2", "content": "normal text", "metadata": {}},
    ]
    blocks = llm_rerank._listwise3_candidate_text(hits)
    assert blocks[0] == '<candidate id="1" title="Doc 1">alpha beta gamma</candidate>'
    assert blocks[1] == '<candidate id="2" title="Doc 2">normal text</candidate>'

    _FakeClient.response_content = '{"analysis": "ok", "direct_answers": [1], "useful_context": [], "irrelevant": []}'
    asyncio.run(llm_rerank.rerank_hits("my query", hits))
    assert _FakeClient.last_json["max_tokens"] == 300
    assert _FakeClient.last_json["temperature"] == 0
    assert _FakeClient.last_json["chat_template_kwargs"] == {"enable_thinking": False}
    user_content = _FakeClient.last_json["messages"][1]["content"]
    assert user_content.startswith("Вопрос: my query\n<candidate id=\"1\"")


def test_listwise3_stable_sort_preserves_initial_order_on_equal_scores(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise3")
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0")
    _FakeClient.response_content = (
        '{"analysis": "only 3 is irrelevant", "direct_answers": [], "useful_context": [], "irrelevant": [3]}'
    )
    hits = [
        {"id": "c1", "content": "doc1", "metadata": {}},
        {"id": "c2", "content": "doc2", "metadata": {}},
        {"id": "c3", "content": "doc3", "metadata": {}},
    ]
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert [hit["id"] for hit in result] == ["c1", "c2", "c3"]
    assert result[0]["metadata"]["llm_rerank_score"] == 4.0
    assert result[1]["metadata"]["llm_rerank_score"] == 4.0
    assert result[2]["metadata"]["llm_rerank_score"] == 1.5


def test_listwise3_preserves_tail_hits_beyond_top_n(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK_MODE", "listwise3")
    monkeypatch.setenv("BHM_LLM_RERANK_TOP_N", "2")
    monkeypatch.setenv("BHM_LLM_RERANK_BLEND", "0")
    _FakeClient.response_content = '{"analysis": "c2 direct", "direct_answers": [2], "useful_context": [], "irrelevant": []}'
    hits = [
        {"id": "c1", "content": "doc1", "metadata": {}},
        {"id": "c2", "content": "doc2", "metadata": {}},
        {"id": "c3", "content": "doc3", "metadata": {}},
        {"id": "c4", "content": "doc4", "metadata": {}},
    ]
    result = asyncio.run(llm_rerank.rerank_hits("question", hits))
    assert [hit["id"] for hit in result] == ["c2", "c1", "c3", "c4"]
    assert result[0]["metadata"]["llm_rerank_score"] == 10.0
    assert result[1]["metadata"]["llm_rerank_score"] == 4.0
    assert "llm_rerank_score" not in result[2]["metadata"]
    assert "llm_rerank_score" not in result[3]["metadata"]
