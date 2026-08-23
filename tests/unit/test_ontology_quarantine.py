from __future__ import annotations

import pytest

from blackholememory.ontology_quarantine import ARTIFACT_TYPE
from blackholememory.ontology_quarantine import build_quarantine_artifact
from blackholememory.ontology_quarantine import serialize_quarantine_record
from blackholememory.ontology_registry import OntologyRelationAdmissionReceipt


def _receipt(*, decision: str = "quarantine") -> OntologyRelationAdmissionReceipt:
    return OntologyRelationAdmissionReceipt(
        project="blackholememory",
        schema_digest="a" * 64,
        decision=decision,
        reason_code="ontology_relation_unknown",
        relation="unknown_relation",
        source_type=None,
        target_type=None,
        source_id_digest="b" * 64,
        target_id_digest="c" * 64,
    )


def test_quarantine_artifact_is_deterministic_and_content_free() -> None:
    first = build_quarantine_artifact(_receipt())
    second = build_quarantine_artifact(_receipt())

    assert first == second
    assert first.artifact_type == ARTIFACT_TYPE
    assert first.memory_id is None
    assert first.payload["content_free"] is True
    assert first.payload["link_storage_mutation"] is False
    assert first.payload["qdrant_mutation"] is False
    assert first.payload["mem0_mutation"] is False
    rendered = str(first.to_record())
    assert "mem-source" not in rendered
    assert "mem-target" not in rendered


def test_quarantine_artifact_rejects_allowed_receipt() -> None:
    with pytest.raises(ValueError, match="quarantined"):
        build_quarantine_artifact(_receipt(decision="allow"))


def test_quarantine_serialization_does_not_add_raw_memory_identifiers() -> None:
    record = build_quarantine_artifact(_receipt()).to_record()
    item = serialize_quarantine_record(record)

    assert set(item) == {
        "id", "project", "created_at", "updated_at", "schema_version", "event_digest",
        "schema_digest", "reason_code", "relation", "source_type", "target_type",
        "source_id_digest", "target_id_digest", "content_free", "requires_review",
        "review_state", "execution",
    }
    assert item["execution"]["link_storage_mutation"] is False
