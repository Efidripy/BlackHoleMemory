from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from blackholememory.longmemeval_semantic import LongMemEvalSemanticError
from blackholememory.longmemeval_semantic import _build_authority_records
from blackholememory.longmemeval_semantic import run_longmemeval_qdrant_global_semantic_smoke
from blackholememory.longmemeval_semantic import run_longmemeval_qdrant_global_hybrid_smoke
from blackholememory.longmemeval_semantic import run_longmemeval_qdrant_semantic_smoke


def _admission(dataset_bytes: bytes, *, version: str) -> dict[str, object]:
    core: dict[str, object] = {
        "schema_version": "bhm.evaluation.external-dataset-admission.v1",
        "ok": True,
        "dataset": {
            "suite": "longmemeval",
            "version": version,
            "dataset_digest": hashlib.sha256(dataset_bytes).hexdigest(),
            "source_url_digest": "a" * 64,
            "source_revision": "b" * 40,
            "license_spdx": "MIT",
            "license_evidence_digest": "c" * 64,
        },
        "review": {
            "status": "approved-local-evaluation-only",
            "reviewer_digest": "d" * 64,
            "reviewed_at": "2026-08-28T00:00:00Z",
        },
        "execution": {
            "network": False,
            "dataset_content_emitted": False,
            "model_calls": 0,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "runtime_feature_enabled": False,
        },
    }
    digest = hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**core, "admission_digest": digest}


def _dataset() -> list[dict[str, object]]:
    return [
        {
            "question_id": "case-temporal",
            "question_type": "temporal-reasoning",
            "question": "Which deployment happened after the rollback?",
            "haystack_session_ids": ["s1", "s2"],
            "haystack_sessions": [
                [{"role": "user", "content": "The rollback happened on Monday."}],
                [{"role": "user", "content": "The deployment happened after the rollback on Tuesday."}],
            ],
            "answer_session_ids": ["s2"],
        },
        {
            "question_id": "case-abs_abs",
            "question_type": "single-session-user",
            "question": "What secret preference was never stated?",
            "haystack_session_ids": ["s3"],
            "haystack_sessions": [[{"role": "user", "content": "Only public preferences are recorded."}]],
            "answer_session_ids": [],
        },
    ]


class _FakeEmbedder:
    def embed_batch(self, texts: list[str], memory_action: str = "search") -> list[list[float]]:
        _ = memory_action
        return [[0.25] * 768 for _ in texts]


class _FakeQdrant:
    def __init__(self, *, mismatch_payload: bool = False, raise_on_query: bool = False, score: float = 0.75) -> None:
        self.collections: dict[str, list[object]] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.mismatch_payload = mismatch_payload
        self.raise_on_query = raise_on_query
        self.score = score
        self.query_filters: list[tuple[str, ...]] = []

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.collections

    def create_collection(self, *, collection_name: str, **_kwargs: object) -> None:
        self.created.append(collection_name)
        self.collections[collection_name] = []

    def upsert(self, *, collection_name: str, points: list[object], **_kwargs: object) -> None:
        self.collections[collection_name].extend(points)

    def query_points(self, *, collection_name: str, query_filter: object, **_kwargs: object) -> object:
        if self.raise_on_query:
            raise RuntimeError("query failed")
        keys = tuple(condition.key for condition in query_filter.must)
        self.query_filters.append(keys)
        case_id = next((condition.match.value for condition in query_filter.must if condition.key == "case_id"), None)
        points: list[object] = []
        for point in self.collections[collection_name]:
            payload = dict(point.payload)
            if case_id is not None and payload["case_id"] != case_id:
                continue
            if self.mismatch_payload:
                payload["content_digest"] = "mismatch"
            points.append(SimpleNamespace(payload=payload, score=self.score))
        return SimpleNamespace(points=points)

    def delete_collection(self, collection_name: str) -> None:
        self.deleted.append(collection_name)
        self.collections.pop(collection_name, None)


def _inputs(tmp_path):
    dataset_bytes = json.dumps(_dataset(), ensure_ascii=False).encode("utf-8")
    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    dataset_path.write_bytes(dataset_bytes)
    return dataset_path, _admission(dataset_bytes, version="fixture-v1")


def test_semantic_evaluation_requires_explicit_disposable_qdrant_consent(tmp_path) -> None:
    dataset_path, admission = _inputs(tmp_path)

    with pytest.raises(LongMemEvalSemanticError, match="explicit disposable Qdrant consent"):
        run_longmemeval_qdrant_semantic_smoke(
            dataset_path,
            dataset_version="fixture-v1",
            admission_report=admission,
            embedder=_FakeEmbedder(),
        )


def test_global_semantic_evaluation_requires_an_explicit_abstention_threshold(tmp_path) -> None:
    dataset_path, admission = _inputs(tmp_path)

    with pytest.raises(LongMemEvalSemanticError, match="explicit abstention minimum_score"):
        run_longmemeval_qdrant_semantic_smoke(
            dataset_path,
            dataset_version="fixture-v1",
            admission_report=admission,
            allow_disposable_qdrant=True,
            candidate_scope="global",
            qdrant_client=_FakeQdrant(),
            embedder=_FakeEmbedder(),
        )


def test_semantic_authority_snapshot_deduplicates_identical_repeated_session() -> None:
    item = _dataset()[0]
    item["haystack_session_ids"] = ["s1", "s1"]
    item["haystack_sessions"] = [item["haystack_sessions"][0], item["haystack_sessions"][0]]
    item["answer_session_ids"] = ["s1"]

    records, cases = _build_authority_records((item,), max_cases=1)

    assert len(cases) == 1
    assert len(records["case-temporal"]) == 1


def test_semantic_evaluation_revalidates_sqlite_and_removes_disposable_collection(tmp_path, monkeypatch) -> None:
    dataset_path, admission = _inputs(tmp_path)
    client = _FakeQdrant()
    monkeypatch.setattr("blackholememory.longmemeval_semantic._collection_name", lambda: "bhm_eval_lme_fixture")

    result = run_longmemeval_qdrant_semantic_smoke(
        dataset_path,
        dataset_version="fixture-v1",
        admission_report=admission,
        allow_disposable_qdrant=True,
        qdrant_client=client,
        embedder=_FakeEmbedder(),
        max_cases=2,
    )

    assert client.created == ["bhm_eval_lme_fixture"]
    assert client.deleted == ["bhm_eval_lme_fixture"]
    assert client.collections == {}
    assert result["report"]["execution"]["temporary_collection_cleanup_verified"] is True
    assert result["report"]["authority_revalidation"] == {
        "checked_candidate_count": 3,
        "rejected_candidate_count": 0,
        "passed": True,
    }
    rendered = json.dumps(result, ensure_ascii=False)
    assert "deployment happened" not in rendered


def test_semantic_evaluation_rejects_qdrant_payload_that_disagrees_with_authority(tmp_path, monkeypatch) -> None:
    dataset_path, admission = _inputs(tmp_path)
    client = _FakeQdrant(mismatch_payload=True)
    monkeypatch.setattr("blackholememory.longmemeval_semantic._collection_name", lambda: "bhm_eval_lme_mismatch")

    result = run_longmemeval_qdrant_semantic_smoke(
        dataset_path,
        dataset_version="fixture-v1",
        admission_report=admission,
        allow_disposable_qdrant=True,
        qdrant_client=client,
        embedder=_FakeEmbedder(),
        max_cases=2,
    )

    assert all(not receipt["retrieved_ids"] for receipt in result["receipts"])
    assert result["report"]["authority_revalidation"]["rejected_candidate_count"] == 3
    assert client.collections == {}


def test_semantic_evaluation_cleans_up_after_qdrant_query_failure(tmp_path, monkeypatch) -> None:
    dataset_path, admission = _inputs(tmp_path)
    client = _FakeQdrant(raise_on_query=True)
    monkeypatch.setattr("blackholememory.longmemeval_semantic._collection_name", lambda: "bhm_eval_lme_failure")

    with pytest.raises(RuntimeError, match="query failed"):
        run_longmemeval_qdrant_semantic_smoke(
            dataset_path,
            dataset_version="fixture-v1",
            admission_report=admission,
            allow_disposable_qdrant=True,
            qdrant_client=client,
            embedder=_FakeEmbedder(),
            max_cases=2,
        )

    assert client.deleted == ["bhm_eval_lme_failure"]
    assert client.collections == {}


def test_global_semantic_route_has_no_case_local_qdrant_filter_or_payload(tmp_path, monkeypatch) -> None:
    dataset_path, admission = _inputs(tmp_path)
    client = _FakeQdrant()
    monkeypatch.setattr("blackholememory.longmemeval_semantic._collection_name", lambda: "bhm_eval_lme_global")

    result = run_longmemeval_qdrant_global_semantic_smoke(
        dataset_path,
        dataset_version="fixture-v1",
        admission_report=admission,
        allow_disposable_qdrant=True,
        qdrant_client=client,
        embedder=_FakeEmbedder(),
        max_cases=2,
        minimum_score=0.40,
    )

    assert client.query_filters == [("project",), ("project",)]
    selection = result["report"]["candidate_selection"]
    assert selection["scope"] == "global"
    assert selection["case_local_filter_used"] is False
    assert selection["project_filter_used"] is True
    assert selection["case_identity_in_qdrant_payload"] is False
    assert selection["minimum_score"] == 0.4
    assert selection["score_required_for_acceptance"] is True
    assert selection["threshold_rejected_candidate_count"] == 0
    assert selection["abstention_policy"] == "no accepted SQLite-revalidated candidate at or above minimum_score"
    assert selection["top_candidate_score_distribution"] == {"count": 2, "min": 0.75, "p50": 0.75, "p95": 0.75, "max": 0.75}
    assert selection["threshold_diagnostics"][-1] == {
        "minimum_score": 0.85,
        "expected_abstention_count": 1,
        "predicted_abstention_count": 2,
        "correct_abstention_count": 1,
        "precision": 0.5,
        "recall": 1.0,
    }
    assert result["report"]["latency_evidence"]["query_embedding_allocation"] == "batch-amortized"
    assert client.collections == {}


def test_global_semantic_route_abstains_when_all_scores_are_below_policy_threshold(tmp_path, monkeypatch) -> None:
    dataset_path, admission = _inputs(tmp_path)
    client = _FakeQdrant(score=0.10)
    monkeypatch.setattr("blackholememory.longmemeval_semantic._collection_name", lambda: "bhm_eval_lme_threshold")

    result = run_longmemeval_qdrant_global_semantic_smoke(
        dataset_path,
        dataset_version="fixture-v1",
        admission_report=admission,
        allow_disposable_qdrant=True,
        qdrant_client=client,
        embedder=_FakeEmbedder(),
        max_cases=2,
        minimum_score=0.40,
    )

    assert all(receipt["abstained"] is True for receipt in result["receipts"])
    assert result["report"]["candidate_selection"]["threshold_rejected_candidate_count"] == 6
    assert result["report"]["capability_metrics"]["abstention"] == {
        "expected_count": 1,
        "predicted_count": 2,
        "correct_count": 1,
        "precision": 0.5,
        "recall": 1.0,
    }
    assert client.collections == {}


def test_global_hybrid_route_fuses_bounded_signals_without_case_local_filter(tmp_path, monkeypatch) -> None:
    dataset_path, admission = _inputs(tmp_path)
    client = _FakeQdrant()
    monkeypatch.setattr("blackholememory.longmemeval_semantic._collection_name", lambda: "bhm_eval_lme_hybrid")

    result = run_longmemeval_qdrant_global_hybrid_smoke(
        dataset_path,
        dataset_version="fixture-v1",
        admission_report=admission,
        allow_disposable_qdrant=True,
        qdrant_client=client,
        embedder=_FakeEmbedder(),
        max_cases=2,
        k=2,
        candidate_limit=3,
        minimum_score=0.40,
    )

    assert client.query_filters == [("project",), ("project",)]
    selection = result["report"]["candidate_selection"]
    assert selection["scope"] == "global"
    assert selection["strategy"] == "hybrid-rrf"
    assert selection["candidate_limit"] == 3
    assert selection["case_identity_in_qdrant_payload"] is False
    assert result["report"]["execution"]["route"] == "bhm-qdrant-disposable-semantic-global-hybrid.v1"
    assert all(len(receipt["retrieved_ids"]) <= 2 for receipt in result["receipts"])
    assert client.collections == {}
