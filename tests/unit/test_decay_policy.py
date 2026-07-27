from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from blackholememory.mem0_adapter import decay_lambda_for_payload
from blackholememory.mem0_adapter import memory_decay_score


def test_decay_policy_prefers_semantic_type_then_memory_type_then_default():
    assert decay_lambda_for_payload({"semantic_type": "architecture", "memory_type": "log"}) == pytest.approx(0.025)
    assert decay_lambda_for_payload({"memory_type": "workflow"}) == pytest.approx(0.08)
    assert decay_lambda_for_payload({}) == pytest.approx(0.05)
    assert decay_lambda_for_payload({"decay_lambda_per_day": 0.2}) == pytest.approx(0.2)
    assert decay_lambda_for_payload({"memory_type": "architecture", "metadata": {"semantic_type": "error"}}) == pytest.approx(0.12)


def test_transient_error_knowledge_decays_faster_than_architecture():
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    old = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    error_score = memory_decay_score(
        {"semantic_type": "error", "importance_score": 5, "access_count": 1, "last_accessed_at": old},
        raw_qdrant_score=1.0,
        now=now,
    )
    architecture_score = memory_decay_score(
        {"semantic_type": "architecture", "importance_score": 5, "access_count": 1, "last_accessed_at": old},
        raw_qdrant_score=1.0,
        now=now,
    )

    assert error_score < architecture_score
