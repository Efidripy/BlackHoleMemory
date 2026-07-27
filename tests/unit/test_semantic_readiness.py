from __future__ import annotations

from blackholememory.semantic_readiness import SemanticReadinessCache
from blackholememory.semantic_readiness import build_readiness_key
from blackholememory.semantic_readiness import evaluate_semantic_readiness
from blackholememory.semantic_readiness import project_warmup_state


def _kwargs(**overrides):
    payload = {
        "project": "sojmieblo",
        "graph_snapshot_id": "graph-1",
        "graph_digest": "g" * 64,
        "current_graph_snapshot_id": "graph-1",
        "graph_repository_snapshot_id": "repo-1",
        "current_repository_snapshot_id": "repo-1",
        "repository_snapshot_digest": "r" * 64,
        "parser_registry_digest": "p" * 64,
        "embedding_contract_digest": "e" * 64,
        "provider_ready": True,
        "runtime_slo_status": "healthy",
        "source_row_count": 3,
        "selected_count": 3,
        "projected_count": 3,
        "projection_pending": 0,
        "projection_failed": 0,
        "skipped_count": 0,
    }
    payload.update(overrides)
    return payload


def test_readiness_passes_only_when_epoch_and_projection_are_complete() -> None:
    receipt = evaluate_semantic_readiness(**_kwargs())
    assert receipt["ready"] is True
    assert receipt["request_status"] == "ready"
    assert receipt["freshness"] == "fresh"
    assert receipt["execution"]["provider_called"] is False


def test_readiness_fails_closed_on_stale_graph_without_provider_activation() -> None:
    receipt = evaluate_semantic_readiness(**_kwargs(current_graph_snapshot_id="graph-2"))
    assert receipt["ready"] is False
    assert receipt["request_status"] == "not_ready"
    assert receipt["requires_operator_projection"] is True
    assert "graph_snapshot_stale" in receipt["failures"]
    assert receipt["execution"]["model_started"] is False
    assert receipt["execution"]["network_called"] is False


def test_readiness_requires_operator_projection_when_counts_or_outbox_drift() -> None:
    receipt = evaluate_semantic_readiness(
        **_kwargs(projected_count=2, projection_pending=1, projection_failed=0)
    )
    assert receipt["ready"] is False
    assert receipt["requires_operator_projection"] is True
    assert "projection_point_completeness_mismatch" in receipt["failures"]
    assert "projection_outbox_not_drained" in receipt["failures"]


def test_readiness_requires_explicit_provider_warmup() -> None:
    receipt = evaluate_semantic_readiness(**_kwargs(provider_ready=False))
    assert receipt["ready"] is False
    assert receipt["requires_operator_warmup"] is True
    assert "provider_warmup_not_ready" in receipt["failures"]


def test_readiness_requires_project_warmup_when_memory_warmup_is_enabled() -> None:
    receipt = evaluate_semantic_readiness(
        **_kwargs(project_warmup_enabled=True, project_warmup_ready=False)
    )
    assert receipt["ready"] is False
    assert receipt["requires_operator_warmup"] is True
    assert "project_provider_warmup_not_ready" in receipt["failures"]
    assert receipt["provider"]["project_warmup_enabled"] is True


def test_readiness_passes_for_explicitly_warmed_project() -> None:
    receipt = evaluate_semantic_readiness(
        **_kwargs(project_warmup_enabled=True, project_warmup_ready=True)
    )
    assert receipt["ready"] is True
    assert receipt["provider"]["project_warmup_ready"] is True


def test_project_warmup_state_distinguishes_warmed_skipped_and_unlisted() -> None:
    status = {
        "memory_warmup_enabled": True,
        "memory_projects": ["SojMieblo"],
        "memory_skipped_projects": ["Bonsai-demo"],
    }
    assert project_warmup_state("sojmieblo", status) == (True, True, "warmed")
    assert project_warmup_state("BONSAI-DEMO", status) == (True, False, "skipped")
    assert project_warmup_state("mcpsrv", status) == (True, False, "unlisted")
    assert project_warmup_state("mcpsrv", {"memory_warmup_enabled": False}) == (False, None, "disabled")


def test_readiness_cache_is_digest_keyed_and_ttl_bounded() -> None:
    key = build_readiness_key(
        project="demo",
        graph_snapshot_id="graph-1",
        graph_digest="g" * 64,
        repository_snapshot_digest="r" * 64,
        parser_registry_digest="p" * 64,
        embedding_contract_digest="e" * 64,
        source_row_count=1,
        selected_count=1,
        projected_count=1,
        projection_pending=0,
        projection_failed=0,
    )
    cache = SemanticReadinessCache(ttl_seconds=5)
    cache.put(key, {"ready": True}, now=10)
    assert cache.get(key, now=12)["cache_hit"] is True
    assert cache.get(key, now=16) is None


def test_readiness_key_changes_when_provider_or_project_warmup_changes() -> None:
    base = dict(
        project="demo",
        graph_snapshot_id="graph-1",
        graph_digest="g" * 64,
        repository_snapshot_digest="r" * 64,
        parser_registry_digest="p" * 64,
        embedding_contract_digest="e" * 64,
        source_row_count=1,
        selected_count=1,
        projected_count=1,
        projection_pending=0,
        projection_failed=0,
    )
    ready = build_readiness_key(**base, provider_ready=True, project_warmup_enabled=True, project_warmup_ready=True)
    cold = build_readiness_key(**base, provider_ready=True, project_warmup_enabled=True, project_warmup_ready=False)
    provider_down = build_readiness_key(**base, provider_ready=False, project_warmup_enabled=True, project_warmup_ready=False)
    assert len({ready, cold, provider_down}) == 3
