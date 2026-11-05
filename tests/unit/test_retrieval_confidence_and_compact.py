from __future__ import annotations

import json

from blackholememory.memory_search_compact import compact_search_response
from blackholememory.retrieval_confidence import AnswerabilityThresholds, assess_answerability
from blackholememory import bhm_mcp_memory


def _hit(score: float, content: str, *, channels=None, lexical_rank=None):
    metadata = {"raw_qdrant_score": score, "fusion_channels": channels or [], "lexical_rank": lexical_rank}
    return {"id": f"m-{score}", "title": "Deploy Ubuntu", "content": content, "score": score, "metadata": metadata}


def test_answerability_reports_confident_weak_none_and_empty():
    confident = assess_answerability("deploy Ubuntu", [_hit(0.95, "Deploy Ubuntu with systemd", channels=["lexical", "semantic"], lexical_rank=1)])
    weak = assess_answerability("deploy Ubuntu", [_hit(0.4, "Release signing")])
    none_hit = _hit(0.01, "PII audit")
    none_hit["title"] = "Audit"
    none = assess_answerability("deploy Ubuntu", [none_hit])
    empty = assess_answerability("deploy Ubuntu", [])
    assert confident["status"] == "confident"
    assert weak["status"] == "weak"
    assert none["status"] == "none"
    assert empty["reason"] == "no_candidates"


def test_compact_response_bounds_fields_and_size():
    content = "слово " * 600
    response = {
        "total": 1,
        "limit": 5,
        "offset": 0,
        "query": "слово",
        "memories": [{
            "id": "m1", "title": "Заметка", "project": "demo", "type": "workflow", "memory_class": "general",
            "updated_at": "2026-01-01", "score": 0.9, "fusion_channels": ["lexical"], "content": content,
            "files": ["a.py", "b.py", "c.py", "d.py"], "upsert_key": "u1", "lifecycle": "active",
            "metadata": {"huge": "x" * 10000, "semantic_rank": 2, "lexical_rank": 1},
        }],
        "retrieval": {"mode": "federated", "local_collection": "l", "global_collection": "g", "query_plan": {"huge": True}},
        "side_effects": {"read_only": True},
    }
    compact = compact_search_response(response, snippet_chars=80)
    assert compact["detail"] == "compact"
    assert len(compact["memories"][0]["snippet"]) <= 80
    assert compact["memories"][0]["has_more"] is True
    assert compact["memories"][0]["files"] == ["a.py", "b.py", "c.py"]
    assert compact["memories"][0]["semantic_rank"] == 2
    assert compact["memories"][0]["lexical_rank"] == 1
    assert "metadata" not in compact["memories"][0]
    assert len(json.dumps(compact, ensure_ascii=False)) < 4096


def test_compact_cross_project_keeps_only_summary():
    response = {
        "total": 0,
        "limit": 5,
        "offset": 0,
        "query": "q",
        "memories": [],
        "cross_project": {
            "projects_searched": 3,
            "projects_with_hits": 1,
            "skipped": [{"project": "slow", "reason": "timeout"}],
            "per_project_top": {"p1": 0.9},
            "project_diagnostics": {"p1": {"status": "hits"}},
        },
    }
    compact = compact_search_response(response)
    assert compact["cross_project"] == {
        "projects_searched": 3,
        "projects_with_hits": 1,
        "skipped": [{"project": "slow", "reason": "timeout"}],
    }
    assert "per_project_top" not in compact["cross_project"]
    assert "project_diagnostics" not in compact["cross_project"]


def test_thresholds_read_environment(monkeypatch):
    monkeypatch.setenv("BHM_ANSWERABILITY_CONFIDENT_SCORE", "0.99")
    result = assess_answerability("x", [_hit(0.9, "x")])
    assert result["status"] != "confident"


def test_threshold_dataclass_changes_status():
    memories = [_hit(0.9, "Deploy Ubuntu")]
    strict = assess_answerability(
        "deploy Ubuntu",
        memories,
        thresholds=AnswerabilityThresholds(confident_score=0.95),
    )
    relaxed = assess_answerability(
        "deploy Ubuntu",
        memories,
        thresholds=AnswerabilityThresholds(confident_score=0.5),
    )
    assert strict["status"] != relaxed["status"]
    assert relaxed["status"] == "confident"


def test_mcp_optional_fields_are_only_sent_when_set(monkeypatch):
    seen = []
    monkeypatch.setattr(bhm_mcp_memory._base, "_post", lambda path, body: seen.append(body) or body)
    bhm_mcp_memory.bhm_search(query="x")
    assert "abstain" not in seen[-1]
    assert seen[-1]["detail"] == "compact"
    bhm_mcp_memory.bhm_search(query="x", abstain=True, detail="compact")
    assert seen[-1]["abstain"] is True and seen[-1]["detail"] == "compact"
    bhm_mcp_memory.bhm_search(query="x", detail="full")
    assert seen[-1]["detail"] == "full"


def test_bhm_search_defaults_to_compact(monkeypatch):
    seen = []
    monkeypatch.setattr(bhm_mcp_memory._base, "_post", lambda path, body: seen.append(body) or body)
    bhm_mcp_memory.bhm_search(query="search default")
    assert seen[-1]["detail"] == "compact"

    bhm_mcp_memory.bhm_search(query="search null", detail=None)
    assert seen[-1]["detail"] == "compact"

    bhm_mcp_memory.bhm_search(query="search full", detail="full")
    assert seen[-1]["detail"] == "full"


def test_compact_snippet_centres_on_the_densest_query_window():
    filler = "вводный текст без сути " * 40
    content = filler + "Запись оператор использует BHM_ADMIN_CAPABILITY через MCP. " + filler
    response = {
        "total": 1, "limit": 5, "offset": 0, "query": "Как bhm_remember пишет память оператор MCP?",
        "memories": [{"id": "m1", "title": "T", "content": content, "score": 0.9}],
    }
    snippet = compact_search_response(response, snippet_chars=200)["memories"][0]["snippet"]
    assert "BHM_ADMIN_CAPABILITY" in snippet


def test_compact_snippet_excludes_trailing_questions():
    filler = "Суть карточки и полезная архитектурная информация. " * 8
    content = (
        filler
        + "\n\nВопросы: как устроена архитектура? какие компоненты важны? где найти документацию?"
    )
    response = {
        "total": 1,
        "limit": 5,
        "offset": 0,
        "query": "какие компоненты важны архитектура документация",
        "memories": [{"id": "m1", "title": "Архитектура", "content": content, "score": 0.9}],
    }
    compact = compact_search_response(response, snippet_chars=200)
    snippet = compact["memories"][0]["snippet"]
    assert "Вопросы:" not in snippet
    assert "Суть карточки" in snippet
    assert "Вопросы:" in response["memories"][0]["content"]


def test_compact_snippet_fallback_when_content_is_only_questions():
    content = "Вопросы: единственный вопрос карточки?"
    response = {
        "total": 1,
        "limit": 5,
        "offset": 0,
        "query": "единственный вопрос",
        "memories": [{"id": "m1", "title": "Вопросы", "content": content, "score": 0.8}],
    }
    compact = compact_search_response(response, snippet_chars=180)
    snippet = compact["memories"][0]["snippet"]
    assert "Вопросы:" in snippet


def test_compact_snippet_multiple_questions_markers():
    content = (
        "Первый раздел.\nВопросы: черновик вопросов\n\n"
        "Второй актуальный раздел с важными данными.\n\n"
        "Вопросы: финальный блок вопросов? как делать?"
    )
    response = {
        "total": 1,
        "limit": 5,
        "offset": 0,
        "query": "финальный блок вопросов как делать",
        "memories": [{"id": "m1", "title": "Разделы", "content": content, "score": 0.9}],
    }
    compact = compact_search_response(response, snippet_chars=200)
    snippet = compact["memories"][0]["snippet"]
    assert "финальный блок вопросов" not in snippet
    assert "Второй актуальный раздел" in snippet


def test_none_score_ceiling_keeps_high_score_hits_out_of_none():
    hit = _hit(0.75, "Signer trust registry policy")
    hit["title"] = "Registry"
    query = "где задаётся пустой реестр подписантов"
    assert assess_answerability(query, [hit])["status"] == "none"
    capped = assess_answerability(query, [hit], thresholds=AnswerabilityThresholds(none_score_ceiling=0.70))
    assert capped["status"] == "weak"


def test_common_query_words_do_not_count_as_coverage():
    def hit(index, text):
        return {"id": f"m{index}", "title": f"Rule {index}", "content": text, "score": 0.75, "metadata": {}}

    generic = "правильно сервер работает всегда"
    memories = [hit(i, generic) for i in range(6)]
    result = assess_answerability("как правильно деплоить сервер на ubuntu", memories)
    assert result["signals"]["lexical_overlap"] <= 0.25
    assert result["status"] == "none"


def test_semantic_ranked_rescue_handles_paraphrase_when_enabled(monkeypatch):
    hit = _hit(0.81, "Плановое обновления", channels=["semantic"])
    hit["metadata"].update({"semantic_score": 0.81, "semantic_rank": 2})
    query = "как подтянуть свежие обновления пакетов"

    assert assess_answerability(query, [hit])["status"] == "none"
    monkeypatch.setenv("BHM_ANSWERABILITY_SEMANTIC_RESCUE_SCORE", "0.75")
    monkeypatch.setenv("BHM_ANSWERABILITY_SEMANTIC_RESCUE_RANK", "3")
    result = assess_answerability(query, [hit])
    assert result["status"] == "weak"
    assert result["reason"] == "semantic_ranked_match"


def test_semantic_rescue_keeps_unranked_no_answer_as_none(monkeypatch):
    hit = _hit(0.81, "Архивирование логов", channels=["semantic"])
    hit["metadata"]["semantic_score"] = 0.81
    query = "как подтянуть свежие обновления пакетов"
    monkeypatch.setenv("BHM_ANSWERABILITY_SEMANTIC_RESCUE_SCORE", "0.75")
    monkeypatch.setenv("BHM_ANSWERABILITY_SEMANTIC_RESCUE_RANK", "3")
    assert assess_answerability(query, [hit])["status"] == "none"


def test_semantic_rescue_memory_type_gate_is_opt_in(monkeypatch):
    hit = _hit(0.81, "Плановое обновления", channels=["semantic"])
    hit["metadata"].update({"semantic_score": 0.81, "semantic_rank": 1, "memory_type": "reference"})
    query = "как подтянуть свежие обновления пакетов"
    monkeypatch.setenv("BHM_ANSWERABILITY_SEMANTIC_RESCUE_SCORE", "0.75")
    monkeypatch.setenv("BHM_ANSWERABILITY_SEMANTIC_RESCUE_RANK", "3")
    assert assess_answerability(query, [hit])["status"] == "weak"
    monkeypatch.setenv("BHM_ANSWERABILITY_SEMANTIC_RESCUE_MEMORY_TYPES", "fact,pattern")
    assert assess_answerability(query, [hit])["status"] == "none"


def test_compact_upsert_key_falls_back_to_metadata_when_item_value_is_null():
    response = {
        "total": 1, "limit": 5, "offset": 0, "query": "q",
        "memories": [{"id": "m1", "title": "t", "content": "text", "upsert_key": None,
                      "metadata": {"upsert_key": "from-metadata"}}],
    }
    assert compact_search_response(response)["memories"][0]["upsert_key"] == "from-metadata"


def test_answerability_confident_with_high_score_coverage_and_rerank_grade():
    hit = _hit(0.95, "Deploy Ubuntu with systemd", channels=["lexical", "semantic"], lexical_rank=1)
    hit["metadata"]["llm_rerank_score"] = 10.0
    result = assess_answerability(
        "deploy Ubuntu",
        [hit],
        thresholds=AnswerabilityThresholds(rerank_rescue_score=5.0),
    )
    assert result["status"] == "confident"
    assert result["reason"] == "strong_top_match"


def test_answerability_low_top_score_with_high_rerank_grade_gives_weak():
    hit = _hit(0.4, "Deploy Ubuntu with systemd", channels=["lexical", "semantic"], lexical_rank=1)
    hit["metadata"]["llm_rerank_score"] = 10.0
    result = assess_answerability(
        "deploy Ubuntu",
        [hit],
        thresholds=AnswerabilityThresholds(rerank_rescue_score=5.0),
    )
    assert result["status"] == "weak"
    assert result["reason"] == "rerank_graded_match"


def test_answerability_low_coverage_with_high_rerank_grade_gives_weak():
    hit = _hit(0.95, "Completely unrelated content")
    hit["title"] = "Unrelated"
    hit["metadata"]["llm_rerank_score"] = 10.0
    result = assess_answerability(
        "deploy Ubuntu",
        [hit],
        thresholds=AnswerabilityThresholds(rerank_rescue_score=5.0),
    )
    assert result["status"] == "weak"
    assert result["reason"] == "rerank_graded_match"


def test_answerability_unreadable_punctuation_query_gives_none():
    hit = _hit(0.95, "Deploy Ubuntu with systemd", channels=["lexical", "semantic"], lexical_rank=1)
    hit["metadata"]["llm_rerank_score"] = 10.0
    result = assess_answerability(
        "????? ??????? ??????",
        [hit],
        thresholds=AnswerabilityThresholds(rerank_rescue_score=5.0),
    )
    assert result["status"] == "none"
    assert result["reason"] == "unreadable_query"
    for expected_key in (
        "top_rerank_score",
        "top_score",
        "score_gap",
        "lexical_overlap",
        "top_lexical_rank",
        "channels_agree",
        "semantic_rescue_rank",
        "semantic_rescue_score",
    ):
        assert expected_key in result["signals"]


def test_answerability_unreadable_fffd_and_valid_queries():
    hit = _hit(0.95, "Deploy Ubuntu with systemd 10048", channels=["lexical", "semantic"], lexical_rank=1)
    hit["metadata"]["llm_rerank_score"] = 10.0

    # строка, где больше трети символов U+FFFD, даёт none
    corrupted = assess_answerability(
        "deploy \ufffd\ufffd\ufffd\ufffd",
        [hit],
        thresholds=AnswerabilityThresholds(rerank_rescue_score=5.0),
    )
    assert corrupted["status"] == "none"
    assert corrupted["reason"] == "unreadable_query"

    # UTF-8, прочитанный как cp866: живой запрос с такой кодировкой получал выдачу
    mojibake = assess_answerability(
        "╨┐╤Ç╨╛ ╨╕ ╤ü╨║╤Ç",
        [hit],
        thresholds=AnswerabilityThresholds(rerank_rescue_score=5.0),
    )
    assert mojibake["reason"] == "unreadable_query"

    # одиночная рамка в осмысленном запросе отказа не вызывает
    framed = assess_answerability(
        "deploy Ubuntu │ systemd",
        [hit],
        thresholds=AnswerabilityThresholds(rerank_rescue_score=5.0),
    )
    assert framed["reason"] != "unreadable_query"

    # обычный запрос на русском и запрос "10048" не дают unreadable_query
    russian = assess_answerability(
        "обычный запрос на русском",
        [hit],
        thresholds=AnswerabilityThresholds(rerank_rescue_score=5.0),
    )
    assert russian["reason"] != "unreadable_query"

    numeric = assess_answerability(
        "10048",
        [hit],
        thresholds=AnswerabilityThresholds(rerank_rescue_score=5.0),
    )
    assert numeric["reason"] != "unreadable_query"


def test_answerability_empty_query_and_empty_memories_give_no_candidates():
    hit = _hit(0.95, "Deploy Ubuntu with systemd")
    empty_memories = assess_answerability("deploy Ubuntu", [])
    assert empty_memories["status"] == "none"
    assert empty_memories["reason"] == "no_candidates"

    empty_query = assess_answerability("", [hit])
    assert empty_query["status"] == "none"
    assert empty_query["reason"] == "no_candidates"

    whitespace_query = assess_answerability("   \t  ", [hit])
    assert whitespace_query["status"] == "none"
    assert whitespace_query["reason"] == "no_candidates"
