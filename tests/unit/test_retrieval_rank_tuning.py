from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from blackholememory.app_core_hit import _rank_hybrid_vector_hits
from blackholememory.mem0_adapter_decay import decay_lambda_for_payload, memory_decay_score
from blackholememory.mem0_adapter_graph import lexical_score
from blackholememory.retrieval_rank_tuning import (
    DEFAULT_TITLE_BOOST_WEIGHT,
    KIND_DECAY_LAMBDA,
    DEFAULT_HOWTO_TYPE_BOOST_WEIGHT,
    DEFAULT_STATE_FRESHNESS_WEIGHT,
    hit_title,
    howto_type_bonus,
    howto_type_boost_enabled,
    howto_type_boost_weight,
    kind_decay_enabled,
    kind_decay_lambda,
    memory_search_kwargs,
    search_semantic_threshold,
    state_freshness_bonus,
    state_freshness_enabled,
    state_freshness_weight,
    title_boost_enabled,
    title_boost_weight,
    title_lexical_bonus,
    search_include_global_enabled,
)


def test_search_include_global_env_defaults_on_and_accepts_disable_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BHM_SEARCH_INCLUDE_GLOBAL", raising=False)
    assert search_include_global_enabled() is True
    for value in ("0", "false", "no", " OFF "):
        monkeypatch.setenv("BHM_SEARCH_INCLUDE_GLOBAL", value)
        assert search_include_global_enabled() is False
    for value in ("1", "true", "yes", "on", "unexpected"):
        monkeypatch.setenv("BHM_SEARCH_INCLUDE_GLOBAL", value)
        assert search_include_global_enabled() is True


def test_search_semantic_threshold_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BHM_SEARCH_SEMANTIC_THRESHOLD", raising=False)
    memory = type("Memory", (), {"search": lambda self, **kwargs: kwargs})()
    assert search_semantic_threshold() is None
    assert "threshold" not in memory_search_kwargs(memory, top_k=5, filters={})

    monkeypatch.setenv("BHM_SEARCH_SEMANTIC_THRESHOLD", "0.0")
    assert search_semantic_threshold() == 0.0
    assert memory_search_kwargs(memory, top_k=5, filters={})["threshold"] == 0.0

    for invalid in ("", "garbage", "-0.1", "1.1", "nan", "inf"):
        monkeypatch.setenv("BHM_SEARCH_SEMANTIC_THRESHOLD", invalid)
        assert search_semantic_threshold() is None
        assert "threshold" not in memory_search_kwargs(memory, top_k=5, filters={})


def test_hit_title_extraction_precedence() -> None:
    # 1. raw_title takes priority
    hit1 = {
        "metadata": {
            "raw_title": "Raw Title",
            "display_title": "Display Title",
        },
        "title": "Root Title",
    }
    assert hit_title(hit1) == "Raw Title"

    # 2. display_title used if raw_title is missing or blank
    hit2 = {
        "metadata": {
            "raw_title": "   ",
            "display_title": "Display Title",
        },
        "title": "Root Title",
    }
    assert hit_title(hit2) == "Display Title"

    hit3 = {
        "metadata": {
            "display_title": "Display Title",
        },
        "title": "Root Title",
    }
    assert hit_title(hit3) == "Display Title"

    # 3. hit["title"] used if metadata has neither
    hit4 = {
        "metadata": {},
        "title": "Root Title",
    }
    assert hit_title(hit4) == "Root Title"

    hit5 = {
        "title": "Root Title",
    }
    assert hit_title(hit5) == "Root Title"

    # 4. Empty string fallback
    assert hit_title({}) == ""
    assert hit_title({"metadata": None}) == ""
    assert hit_title({"metadata": {"raw_title": "", "display_title": " "}}) == ""
    assert hit_title({"title": "   "}) == ""


def test_title_boost_flags_and_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BHM_RANK_TITLE_BOOST", raising=False)
    assert not title_boost_enabled()

    for truthy in ("1", "true", "TRUE", "True", "on", "ON", "  on  "):
        monkeypatch.setenv("BHM_RANK_TITLE_BOOST", truthy)
        assert title_boost_enabled()

    for falsy in ("0", "false", "off", "", "anything_else"):
        monkeypatch.setenv("BHM_RANK_TITLE_BOOST", falsy)
        assert not title_boost_enabled()

    monkeypatch.delenv("BHM_RANK_TITLE_WEIGHT", raising=False)
    assert title_boost_weight() == DEFAULT_TITLE_BOOST_WEIGHT

    monkeypatch.setenv("BHM_RANK_TITLE_WEIGHT", "0.0")
    assert title_boost_weight() == 0.0

    monkeypatch.setenv("BHM_RANK_TITLE_WEIGHT", "10.0")
    assert title_boost_weight() == 10.0

    monkeypatch.setenv("BHM_RANK_TITLE_WEIGHT", "2.75")
    assert title_boost_weight() == 2.75

    for out_of_range in ("-0.1", "10.1", "100", "-5", "not-a-number", "nan", "inf", ""):
        monkeypatch.setenv("BHM_RANK_TITLE_WEIGHT", out_of_range)
        assert title_boost_weight() == DEFAULT_TITLE_BOOST_WEIGHT


def test_title_lexical_bonus_calculation(monkeypatch: pytest.MonkeyPatch) -> None:
    query = "database connection pool leak"

    # No title -> 0.0
    assert title_lexical_bonus(query, {}) == 0.0
    assert title_lexical_bonus(query, {"metadata": {"raw_title": ""}}) == 0.0

    # Title matches query
    hit_matching = {"metadata": {"raw_title": "Database connection pool leak resolution"}}
    raw_lex = lexical_score(query, "Database connection pool leak resolution")
    assert raw_lex > 0.0

    monkeypatch.setenv("BHM_RANK_TITLE_WEIGHT", "2.0")
    assert title_lexical_bonus(query, hit_matching) == pytest.approx(2.0 * raw_lex)

    # Title has no overlap -> 0.0
    hit_unrelated = {"metadata": {"raw_title": "Frontend styling colors"}}
    assert title_lexical_bonus(query, hit_unrelated) == 0.0


def test_kind_decay_flags_and_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BHM_RANK_KIND_DECAY", raising=False)
    assert not kind_decay_enabled()
    assert kind_decay_lambda({"memory_type": "rule"}) is None

    expected_table = {
        "rule": 0.003,
        "playbook": 0.005,
        "reference": 0.01,
        "decision": 0.02,
        "pitfall": 0.02,
        "runbook": 0.01,
        "lesson": 0.02,
        "state": 0.15,
        "receipt": 0.2,
    }
    assert KIND_DECAY_LAMBDA == expected_table

    for truthy in ("1", "true", "TRUE", "on", "ON"):
        monkeypatch.setenv("BHM_RANK_KIND_DECAY", truthy)
        assert kind_decay_enabled()

    for kind, expected_rate in expected_table.items():
        assert kind_decay_lambda({"memory_type": kind}) == pytest.approx(expected_rate)
        assert kind_decay_lambda({"type": kind}) == pytest.approx(expected_rate)
        assert kind_decay_lambda({"metadata": {"memory_type": kind}}) == pytest.approx(expected_rate)
        assert kind_decay_lambda({"metadata": {"type": kind}}) == pytest.approx(expected_rate)

    # Unknown type
    assert kind_decay_lambda({"memory_type": "unknown_type"}) is None
    assert kind_decay_lambda({}) is None


def test_decay_lambda_for_payload_wiring_and_override_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag disabled: rule uses fallback default
    monkeypatch.delenv("BHM_RANK_KIND_DECAY", raising=False)
    assert decay_lambda_for_payload({"memory_type": "rule"}) == pytest.approx(0.05)

    # Flag enabled: rule uses 0.003
    monkeypatch.setenv("BHM_RANK_KIND_DECAY", "1")
    assert decay_lambda_for_payload({"memory_type": "rule"}) == pytest.approx(0.003)
    assert decay_lambda_for_payload({"type": "playbook"}) == pytest.approx(0.005)
    assert decay_lambda_for_payload({"metadata": {"memory_type": "state"}}) == pytest.approx(0.15)

    # Explicit override retains highest priority
    assert decay_lambda_for_payload({"decay_lambda_per_day": 0.08, "memory_type": "rule"}) == pytest.approx(0.08)
    assert decay_lambda_for_payload({"metadata": {"decay_lambda_per_day": 0.06}, "memory_type": "rule"}) == pytest.approx(0.06)

    # Unknown kind falls back to existing taxonomy/default
    assert decay_lambda_for_payload({"memory_type": "workflow"}) == pytest.approx(0.08)
    assert decay_lambda_for_payload({"semantic_type": "architecture", "memory_type": "unknown"}) == pytest.approx(0.025)
    assert decay_lambda_for_payload({}) == pytest.approx(0.05)


def test_retention_over_time_rule_vs_workflow_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BHM_RANK_KIND_DECAY", "1")

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t_365 = now + timedelta(days=365)
    now_iso = now.isoformat().replace("+00:00", "Z")

    rule_payload = {
        "memory_type": "rule",
        "importance_score": 5,
        "access_count": 1,
        "last_accessed_at": now_iso,
    }
    workflow_payload = {
        "memory_type": "workflow",
        "importance_score": 5,
        "access_count": 1,
        "last_accessed_at": now_iso,
    }

    score_rule_365 = memory_decay_score(rule_payload, 1.0, now=t_365)
    score_workflow_365 = memory_decay_score(workflow_payload, 1.0, now=t_365)

    # Rule (lambda=0.003) retains > 30% of its initial score after 1 year.
    # Workflow (lambda=0.08) retains < 1e-10.
    assert score_rule_365 > 0.3
    assert score_workflow_365 < 1e-10
    assert score_rule_365 > score_workflow_365 * 1000

    # State (lambda=0.15) decays rapidly over 30 days
    t_30 = now + timedelta(days=30)
    state_payload = {
        "memory_type": "state",
        "importance_score": 5,
        "access_count": 1,
        "last_accessed_at": now_iso,
    }
    default_payload = {
        "importance_score": 5,
        "access_count": 1,
        "last_accessed_at": now_iso,
    }

    score_state_30 = memory_decay_score(state_payload, 1.0, now=t_30)
    score_default_30 = memory_decay_score(default_payload, 1.0, now=t_30)
    score_rule_30 = memory_decay_score(rule_payload, 1.0, now=t_30)

    assert score_state_30 < score_default_30
    assert score_state_30 < score_rule_30


def test_ranking_disabled_preserves_order_and_scores_without_title_bonus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BHM_RANK_TITLE_BOOST", raising=False)

    query = "postgres pool configuration"
    candidates = [
        {
            "id": "cand-1",
            "score": 0.90,
            "content": "postgres pool configuration parameters and size limits",
            "metadata": {"raw_title": "Postgres Pool Guide"},
        },
        {
            "id": "cand-2",
            "score": 0.85,
            "content": "database server settings and pool setup",
            "metadata": {"raw_title": "Database Server"},
        },
        {
            "id": "cand-3",
            "score": 0.70,
            "content": "unrelated logging details and diagnostic flags",
            "metadata": {"raw_title": "Diagnostics"},
        },
    ]

    ranked = _rank_hybrid_vector_hits(query, candidates)
    assert [hit["id"] for hit in ranked] == ["cand-1", "cand-2", "cand-3"]

    for hit in ranked:
        assert "title_bonus" not in hit["metadata"]
        assert "semantic_rank" in hit["metadata"]
        assert "lexical_rank" in hit["metadata"]


def test_ranking_title_boost_promotes_title_match(monkeypatch: pytest.MonkeyPatch) -> None:
    query = "postgres backup restore"

    # Candidate A: higher semantic score, query words in text, generic title
    cand_a = {
        "id": "cand-a",
        "score": 0.86,
        "content": "postgres backup restore procedures and maintenance tasks postgres backup restore",
        "metadata": {"raw_title": "General Operations"},
    }

    # Candidate B: slightly lower semantic score, short text, exact query match in title
    cand_b = {
        "id": "cand-b",
        "score": 0.85,
        "content": "routine operations checklist",
        "metadata": {"raw_title": "postgres backup restore"},
    }

    # Candidate C: lower semantic score, partial overlap
    cand_c = {
        "id": "cand-c",
        "score": 0.80,
        "content": "postgres backup",
        "metadata": {"raw_title": "Backup scripts"},
    }

    # 1. Without title boost: cand_a ranks #1, cand_b ranks #2
    monkeypatch.delenv("BHM_RANK_TITLE_BOOST", raising=False)
    ranked_off = _rank_hybrid_vector_hits(query, [cand_a, cand_b, cand_c])
    assert [hit["id"] for hit in ranked_off] == ["cand-a", "cand-b", "cand-c"]
    for hit in ranked_off:
        assert "title_bonus" not in hit["metadata"]

    # 2. With title boost enabled: cand_b receives strong title bonus and overtakes cand_a for #1
    monkeypatch.setenv("BHM_RANK_TITLE_BOOST", "1")
    monkeypatch.setenv("BHM_RANK_TITLE_WEIGHT", "2.0")
    ranked_on = _rank_hybrid_vector_hits(query, [cand_a, cand_b, cand_c])
    assert [hit["id"] for hit in ranked_on] == ["cand-b", "cand-a", "cand-c"]
    assert ranked_on[0]["metadata"]["title_bonus"] > 0.0
    assert ranked_on[1]["metadata"]["title_bonus"] == 0.0


def test_hit_title_reads_the_nested_stored_metadata_of_vector_hits():
    hit = {"title": "content prefix", "metadata": {"metadata": {"raw_title": "Explicit stored title"}}}
    assert hit_title(hit) == "Explicit stored title"


def test_howto_type_boost_is_opt_in_and_promotes_playbook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BHM_RANK_HOWTO_TYPE_BOOST", raising=False)
    assert not howto_type_boost_enabled()
    assert howto_type_boost_weight() == DEFAULT_HOWTO_TYPE_BOOST_WEIGHT
    hit = {"metadata": {"memory_type": "playbook"}}
    assert howto_type_bonus("howto", hit) == DEFAULT_HOWTO_TYPE_BOOST_WEIGHT
    assert howto_type_bonus("state", hit) == 0.0

    query = "Как перезапустить API?"
    rule = {
        "id": "rule",
        "score": 0.88,
        "content": "инструкция по перезапуску API",
        "metadata": {"memory_type": "rule"},
    }
    playbook = {
        "id": "playbook",
        "score": 0.88,
        "content": "инструкция по перезапуску API",
        "metadata": {"memory_type": "playbook"},
    }
    monkeypatch.setenv("BHM_RANK_HOWTO_TYPE_BOOST", "1")
    ranked = _rank_hybrid_vector_hits(query, [rule, playbook])
    assert [item["id"] for item in ranked] == ["playbook", "rule"]
    assert ranked[0]["metadata"]["howto_type_bonus"] == DEFAULT_HOWTO_TYPE_BOOST_WEIGHT


def test_state_freshness_bonus_prefers_new_state_and_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BHM_RANK_STATE_FRESHNESS", raising=False)
    assert not state_freshness_enabled()
    assert state_freshness_weight() == DEFAULT_STATE_FRESHNESS_WEIGHT
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    fresh = {"metadata": {"memory_type": "state", "updated_at": "2026-11-01T00:00:00Z"}}
    old = {"metadata": {"memory_type": "state", "updated_at": "2026-01-01T00:00:00Z"}}
    assert state_freshness_bonus("howto", fresh, now=now) == 0.0
    assert state_freshness_bonus("state", fresh, now=now) > state_freshness_bonus("state", old, now=now)

    query = "Что сейчас с проектом?"
    stale = {
        "id": "stale",
        "score": 0.88,
        "content": "состояние проекта",
        "metadata": {"memory_type": "state", "updated_at": "2026-01-01T00:00:00Z"},
    }
    current = {
        "id": "current",
        "score": 0.88,
        "content": "состояние проекта",
        "metadata": {"memory_type": "state", "updated_at": "2026-11-01T00:00:00Z"},
    }
    monkeypatch.setenv("BHM_RANK_STATE_FRESHNESS", "1")
    ranked = _rank_hybrid_vector_hits(query, [stale, current])
    assert [item["id"] for item in ranked] == ["current", "stale"]
    assert ranked[0]["metadata"]["state_freshness_bonus"] > ranked[1]["metadata"]["state_freshness_bonus"]
