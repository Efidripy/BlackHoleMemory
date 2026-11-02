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
            "metadata": {"huge": "x" * 10000},
        }],
        "retrieval": {"mode": "federated", "local_collection": "l", "global_collection": "g", "query_plan": {"huge": True}},
        "side_effects": {"read_only": True},
    }
    compact = compact_search_response(response, snippet_chars=80)
    assert compact["detail"] == "compact"
    assert len(compact["memories"][0]["snippet"]) <= 80
    assert compact["memories"][0]["has_more"] is True
    assert compact["memories"][0]["files"] == ["a.py", "b.py", "c.py"]
    assert "metadata" not in compact["memories"][0]
    assert len(json.dumps(compact, ensure_ascii=False)) < 4096


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
    assert "abstain" not in seen[-1] and "detail" not in seen[-1]
    bhm_mcp_memory.bhm_search(query="x", abstain=True, detail="compact")
    assert seen[-1]["abstain"] is True and seen[-1]["detail"] == "compact"


def test_compact_snippet_centres_on_the_densest_query_window():
    filler = "вводный текст без сути " * 40
    content = filler + "Запись оператор использует BHM_ADMIN_CAPABILITY через MCP. " + filler
    response = {
        "total": 1, "limit": 5, "offset": 0, "query": "Как bhm_remember пишет память оператор MCP?",
        "memories": [{"id": "m1", "title": "T", "content": content, "score": 0.9}],
    }
    snippet = compact_search_response(response, snippet_chars=200)["memories"][0]["snippet"]
    assert "BHM_ADMIN_CAPABILITY" in snippet


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
