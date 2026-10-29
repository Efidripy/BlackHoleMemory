"""Unit tests for retrieval type prior ranking bonus based on question intent."""

from __future__ import annotations

import pytest

from blackholememory.app_core_hit import _rank_hybrid_vector_hits
from blackholememory.retrieval_rank_tuning import (
    DEFAULT_TYPE_PRIOR_WEIGHT,
    TYPE_PRIOR_BY_INTENT,
    hit_memory_type,
    memory_intent,
    type_prior_bonus,
    type_prior_enabled,
    type_prior_weight,
)

# Table of at least 6 questions per intent (Russian and English)
INTENT_TEST_CASES: list[tuple[str, str]] = [
    # howto
    ("Как перезапустить API?", "howto"),
    ("Как настроить репликацию базы данных?", "howto"),
    ("Как правильно конфигурировать пул соединений?", "howto"),
    ("Что делать при переполнении очереди задач?", "howto"),
    ("How to deploy to production?", "howto"),
    ("How do I run tests in docker?", "howto"),
    ("How can I flush the vector index?", "howto"),
    ("Steps to restore snapshot from S3", "howto"),
    # pitfall
    ("Почему упал smoke релиза?", "pitfall"),
    ("Почему падает сервер при нагрузке?", "pitfall"),
    ("Почему не стартует контейнер worker?", "pitfall"),
    ("Ошибка аутентификации в API", "pitfall"),
    ("База данных не работает после перезапуска", "pitfall"),
    ("Why did the release smoke fail?", "pitfall"),
    ("Why does the worker crash on startup?", "pitfall"),
    ("Why is the background job hanging?", "pitfall"),
    ("Authentication failure during login", "pitfall"),
    # decision
    ("Почему решили отказаться от подписи?", "decision"),
    ("Почему выбрали Qdrant вместо Chroma?", "decision"),
    ("Зачем нам нужен отдельный воркер?", "decision"),
    ("Для чего используется LangGraph?", "decision"),
    ("Что решили по поводу схемы миграций?", "decision"),
    ("Why did we choose SQLite over PostgreSQL?", "decision"),
    ("What was decided about token rotation?", "decision"),
    ("Why did we reject Mongo in production?", "decision"),
    # state
    ("Что сейчас с проектом?", "state"),
    ("Текущее состояние базы данных", "state"),
    ("Какой статус задачи FIX-017?", "state"),
    ("Что осталось сделать перед релизом?", "state"),
    ("Что открыто по багам безопасности?", "state"),
    ("Что происходит с очередью сообщений?", "state"),
    ("What is the status of the migration?", "state"),
    ("What is current state of cluster?", "state"),
    ("What is left to implement before launch?", "state"),
    # reference
    ("Как называется модель эмбеддингов?", "reference"),
    ("Где задан таймаут соединения?", "reference"),
    ("Где хранится токен авторизации?", "reference"),
    ("Какая модель используется для поиска?", "reference"),
    ("Какой порт слушает сервер?", "reference"),
    ("Какой таймаут у HTTP клиента?", "reference"),
    ("Какой путь к файлу конфигурации?", "reference"),
    ("Сколько памяти выделено под кэш?", "reference"),
    ("Which port does Qdrant listen on?", "reference"),
    ("Which model is configured for reranking?", "reference"),
    ("Where is the master key stored?", "reference"),
    ("What is the name of the primary model?", "reference"),
    ("How many workers are spawned by default?", "reference"),
    # other
    ("Какая у нас погода?", "other"),
    ("Привет, расскажи о себе", "other"),
    ("Что ты думаешь про погоду в Москве?", "other"),
    ("Good morning everyone", "other"),
    ("What is the weather outside today?", "other"),
    ("Random notes about coffee beans", "other"),
    ("", "other"),
    ("   ", "other"),
]


@pytest.mark.parametrize("query,expected_intent", INTENT_TEST_CASES)
def test_memory_intent_classification(query: str, expected_intent: str) -> None:
    assert memory_intent(query) == expected_intent


def test_intent_distribution_has_at_least_six_per_intent() -> None:
    intents = [expected for _, expected in INTENT_TEST_CASES]
    for required_intent in ("howto", "pitfall", "decision", "state", "reference", "other"):
        count = sum(1 for item in intents if item == required_intent)
        assert count >= 6, f"Expected at least 6 cases for intent '{required_intent}', got {count}"


def test_type_prior_by_intent_mapping() -> None:
    assert TYPE_PRIOR_BY_INTENT == {
        "howto": {"playbook", "runbook", "howto"},
        "pitfall": {"pitfall", "bug", "bugfix", "error"},
        "decision": {"decision", "architecture", "adr"},
        "state": {"state"},
        "reference": {"reference", "architecture"},
    }


def test_type_prior_flags_and_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BHM_RANK_TYPE_PRIOR", raising=False)
    assert type_prior_enabled()

    for truthy in ("1", "true", "TRUE", "True", "on", "ON", "  on  ", "", "anything_else"):
        monkeypatch.setenv("BHM_RANK_TYPE_PRIOR", truthy)
        assert type_prior_enabled()

    for falsy in ("0", "false", "FALSE", "off", "  off  ", "no"):
        monkeypatch.setenv("BHM_RANK_TYPE_PRIOR", falsy)
        assert not type_prior_enabled()

    monkeypatch.delenv("BHM_RANK_TYPE_PRIOR_WEIGHT", raising=False)
    assert type_prior_weight() == DEFAULT_TYPE_PRIOR_WEIGHT
    assert type_prior_weight() == pytest.approx(0.004)

    monkeypatch.setenv("BHM_RANK_TYPE_PRIOR_WEIGHT", "0.0")
    assert type_prior_weight() == 0.0

    monkeypatch.setenv("BHM_RANK_TYPE_PRIOR_WEIGHT", "0.05")
    assert type_prior_weight() == 0.05

    monkeypatch.setenv("BHM_RANK_TYPE_PRIOR_WEIGHT", "0.025")
    assert type_prior_weight() == 0.025

    for out_of_range in ("-0.001", "-1.0", "0.051", "0.1", "100", "not-a-number", "nan", "inf", ""):
        monkeypatch.setenv("BHM_RANK_TYPE_PRIOR_WEIGHT", out_of_range)
        assert type_prior_weight() == DEFAULT_TYPE_PRIOR_WEIGHT


def test_hit_memory_type_extraction_precedence() -> None:
    # 1. metadata.memory_type takes precedence over metadata.type
    hit1 = {
        "metadata": {
            "memory_type": "playbook",
            "type": "rule",
        },
        "type": "state",
    }
    assert hit_memory_type(hit1) == "playbook"

    # 2. metadata.type used if memory_type is absent or blank
    hit2 = {
        "metadata": {
            "memory_type": "   ",
            "type": "runbook",
        },
        "type": "rule",
    }
    assert hit_memory_type(hit2) == "runbook"

    # 3. nested metadata.metadata.type used next
    hit3 = {
        "metadata": {
            "metadata": {
                "type": "decision",
                "memory_type": "architecture",
            },
        },
        "type": "rule",
    }
    assert hit_memory_type(hit3) == "decision"

    # 4. nested metadata.metadata.memory_type used if nested type absent
    hit4 = {
        "metadata": {
            "metadata": {
                "memory_type": "architecture",
            },
        },
        "type": "rule",
    }
    assert hit_memory_type(hit4) == "architecture"

    # 5. hit["type"] root fallback
    hit5 = {
        "type": "reference",
    }
    assert hit_memory_type(hit5) == "reference"

    # 6. Case normalization and trimming
    hit6 = {
        "metadata": {
            "memory_type": "  PLAYBOOK  ",
        },
    }
    assert hit_memory_type(hit6) == "playbook"

    # 7. Empty fallbacks
    assert hit_memory_type({}) == ""
    assert hit_memory_type({"metadata": None}) == ""
    assert hit_memory_type({"metadata": {"memory_type": "", "type": " "}}) == ""


def test_type_prior_bonus_by_intent() -> None:
    weight = 0.004

    # howto intent: playbook, runbook, howto
    assert type_prior_bonus("howto", {"metadata": {"memory_type": "playbook"}}) == weight
    assert type_prior_bonus("howto", {"metadata": {"memory_type": "runbook"}}) == weight
    assert type_prior_bonus("howto", {"metadata": {"memory_type": "howto"}}) == weight
    assert type_prior_bonus("howto", {"metadata": {"memory_type": "rule"}}) == 0.0

    # pitfall intent: pitfall, bug, bugfix, error
    assert type_prior_bonus("pitfall", {"metadata": {"memory_type": "pitfall"}}) == weight
    assert type_prior_bonus("pitfall", {"metadata": {"type": "bug"}}) == weight
    assert type_prior_bonus("pitfall", {"type": "bugfix"}) == weight
    assert type_prior_bonus("pitfall", {"metadata": {"metadata": {"type": "error"}}}) == weight
    assert type_prior_bonus("pitfall", {"metadata": {"memory_type": "playbook"}}) == 0.0

    # decision intent: decision, architecture, adr
    assert type_prior_bonus("decision", {"metadata": {"memory_type": "decision"}}) == weight
    assert type_prior_bonus("decision", {"metadata": {"memory_type": "architecture"}}) == weight
    assert type_prior_bonus("decision", {"metadata": {"type": "adr"}}) == weight
    assert type_prior_bonus("decision", {"metadata": {"memory_type": "rule"}}) == 0.0

    # state intent: state
    assert type_prior_bonus("state", {"metadata": {"memory_type": "state"}}) == weight
    assert type_prior_bonus("state", {"metadata": {"memory_type": "playbook"}}) == 0.0

    # reference intent: reference, architecture
    assert type_prior_bonus("reference", {"metadata": {"memory_type": "reference"}}) == weight
    assert type_prior_bonus("reference", {"metadata": {"memory_type": "architecture"}}) == weight
    assert type_prior_bonus("reference", {"metadata": {"memory_type": "state"}}) == 0.0

    # other intent: always 0.0
    for kind in ("playbook", "bug", "decision", "state", "reference", "rule"):
        assert type_prior_bonus("other", {"metadata": {"memory_type": kind}}) == 0.0


def test_ranking_disabled_preserves_order_and_scores_without_type_prior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BHM_RANK_TYPE_PRIOR", "0")
    monkeypatch.delenv("BHM_RANK_TITLE_BOOST", raising=False)

    query = "Как перезапустить API?"
    cand_rule = {
        "id": "cand-rule",
        "score": 0.88,
        "content": "инструкция по перезапуску API и серверов",
        "metadata": {"memory_type": "rule"},
    }
    cand_playbook = {
        "id": "cand-playbook",
        "score": 0.88,
        "content": "инструкция по перезапуску API и серверов",
        "metadata": {"memory_type": "playbook"},
    }

    ranked = _rank_hybrid_vector_hits(query, [cand_rule, cand_playbook])
    assert [hit["id"] for hit in ranked] == ["cand-rule", "cand-playbook"]
    for hit in ranked:
        assert "type_prior_bonus" not in hit["metadata"]


def test_ranking_type_prior_promotes_playbook_over_rule_for_howto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "Как перезапустить API?"

    cand_rule = {
        "id": "cand-rule",
        "score": 0.88,
        "content": "инструкция по перезапуску API и серверов приложения",
        "metadata": {"memory_type": "rule"},
    }
    cand_playbook = {
        "id": "cand-playbook",
        "score": 0.88,
        "content": "инструкция по перезапуску API и серверов приложения",
        "metadata": {"memory_type": "playbook"},
    }

    # 1. Switched off: cand-rule stays first (initial tie-breaker)
    monkeypatch.setenv("BHM_RANK_TYPE_PRIOR", "0")
    ranked_off = _rank_hybrid_vector_hits(query, [cand_rule, cand_playbook])
    assert [hit["id"] for hit in ranked_off] == ["cand-rule", "cand-playbook"]
    for hit in ranked_off:
        assert "type_prior_bonus" not in hit["metadata"]

    # 2. With flag enabled: cand-playbook gets bonus 0.004 and overtakes cand-rule for #1
    monkeypatch.setenv("BHM_RANK_TYPE_PRIOR", "1")
    monkeypatch.setenv("BHM_RANK_TYPE_PRIOR_WEIGHT", "0.004")
    ranked_on = _rank_hybrid_vector_hits(query, [cand_rule, cand_playbook])
    assert [hit["id"] for hit in ranked_on] == ["cand-playbook", "cand-rule"]
    assert ranked_on[0]["metadata"]["type_prior_bonus"] == pytest.approx(0.004)
    assert ranked_on[1]["metadata"]["type_prior_bonus"] == 0.0


def test_ranking_bonus_does_not_change_order_when_types_do_not_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Query intent is 'other' -> no bonus applied to any candidate
    monkeypatch.setenv("BHM_RANK_TYPE_PRIOR", "1")
    query = "Какая у нас погода?"

    cand_a = {
        "id": "cand-a",
        "score": 0.90,
        "content": "прогноз погоды и сводка температуры",
        "metadata": {"memory_type": "rule"},
    }
    cand_b = {
        "id": "cand-b",
        "score": 0.80,
        "content": "метеорологический отчет",
        "metadata": {"memory_type": "playbook"},
    }

    ranked = _rank_hybrid_vector_hits(query, [cand_a, cand_b])
    assert [hit["id"] for hit in ranked] == ["cand-a", "cand-b"]
    assert ranked[0]["metadata"]["type_prior_bonus"] == 0.0
    assert ranked[1]["metadata"]["type_prior_bonus"] == 0.0


def test_ranking_type_prior_promotes_intents_across_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BHM_RANK_TYPE_PRIOR", "1")
    monkeypatch.setenv("BHM_RANK_TYPE_PRIOR_WEIGHT", "0.004")

    # Pitfall intent promotes 'bug' over 'state'
    query_pitfall = "Почему упал smoke релиза?"
    cand_state = {
        "id": "cand-state",
        "score": 0.85,
        "content": "smoke релиза упал сбой проверки",
        "metadata": {"memory_type": "state"},
    }
    cand_bug = {
        "id": "cand-bug",
        "score": 0.85,
        "content": "smoke релиза упал сбой проверки",
        "metadata": {"memory_type": "bug"},
    }
    ranked_pitfall = _rank_hybrid_vector_hits(query_pitfall, [cand_state, cand_bug])
    assert [hit["id"] for hit in ranked_pitfall] == ["cand-bug", "cand-state"]
    assert ranked_pitfall[0]["metadata"]["type_prior_bonus"] == pytest.approx(0.004)
    assert ranked_pitfall[1]["metadata"]["type_prior_bonus"] == 0.0

    # Decision intent promotes 'adr' over 'rule'
    query_decision = "Почему решили отказаться от подписи?"
    cand_rule = {
        "id": "cand-rule",
        "score": 0.85,
        "content": "решение отказаться от цифровой подписи",
        "metadata": {"memory_type": "rule"},
    }
    cand_adr = {
        "id": "cand-adr",
        "score": 0.85,
        "content": "решение отказаться от цифровой подписи",
        "metadata": {"memory_type": "adr"},
    }
    ranked_decision = _rank_hybrid_vector_hits(query_decision, [cand_rule, cand_adr])
    assert [hit["id"] for hit in ranked_decision] == ["cand-adr", "cand-rule"]
    assert ranked_decision[0]["metadata"]["type_prior_bonus"] == pytest.approx(0.004)
    assert ranked_decision[1]["metadata"]["type_prior_bonus"] == 0.0
