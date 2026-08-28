from __future__ import annotations

import hashlib
import json

from blackholememory.longmemeval_smoke import PROJECT
from blackholememory.longmemeval_smoke import ROUTE
from blackholememory.longmemeval_smoke import run_longmemeval_lexical_smoke


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


def test_longmemeval_smoke_is_bounded_content_free_and_offline(tmp_path) -> None:
    dataset_bytes = json.dumps(_dataset(), ensure_ascii=False).encode("utf-8")
    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    dataset_path.write_bytes(dataset_bytes)
    result = run_longmemeval_lexical_smoke(
        dataset_path,
        dataset_version="fixture-v1",
        admission_report=_admission(dataset_bytes, version="fixture-v1"),
        max_cases=2,
    )

    assert len(result["manifest"]["cases"]) == 2
    assert len(result["receipts"]) == 2
    assert result["report"]["suite"] == "longmemeval"
    assert result["report"]["provenance_and_isolation"]["passed"] is True
    assert result["report"]["execution"] == {
        "network": False,
        "model_calls": 0,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "route": ROUTE,
        "runtime_feature_enabled": False,
    }
    assert all(case["project"] == PROJECT for case in result["manifest"]["cases"])
    assert "deployment happened" not in json.dumps(result["manifest"])
    assert "deployment happened" not in json.dumps(result["receipts"])
