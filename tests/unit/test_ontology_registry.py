from __future__ import annotations

import copy

import pytest

from blackholememory.ontology_registry import OntologyEntityType
from blackholememory.ontology_registry import OntologyRelationWrite
from blackholememory.ontology_registry import OntologyRelationType
from blackholememory.ontology_registry import OntologySchema
from blackholememory.ontology_registry import OntologyRegistryError
from blackholememory.ontology_registry import build_activation_artifact
from blackholememory.ontology_registry import build_registry_artifact
from blackholememory.ontology_registry import admit_relation_write
from blackholememory.ontology_registry import quarantine_unknown
from blackholememory.ontology_registry import resolve_active_schema
from blackholememory.ontology_registry import validate_entity
from blackholememory.ontology_registry import validate_relation


def _schema(**overrides: object) -> OntologySchema:
    values: dict[str, object] = {
        "project": "blackholememory",
        "owner": "operator",
        "activation_status": "declared",
        "entity_types": (
            OntologyEntityType(name="service", aliases=("svc",), attributes={"language": "string"}),
            OntologyEntityType(name="repository"),
        ),
        "relation_types": (
            OntologyRelationType(name="owns", source_types=("service",), target_types=("repository",)),
        ),
    }
    values.update(overrides)
    return OntologySchema.model_validate(values)


def test_registry_digest_is_deterministic_and_aliases_resolve() -> None:
    schema = _schema()
    reordered = _schema(
        entity_types=tuple(reversed(schema.entity_types)),
        relation_types=tuple(reversed(schema.relation_types)),
    )
    assert schema.digest() == reordered.digest()
    assert schema.entity("SVC").name == "service"
    assert schema.relation("owns").name == "owns"


def test_registry_rejects_unknown_relation_endpoint_and_duplicates() -> None:
    with pytest.raises(ValueError, match="unknown entity"):
        _schema(relation_types=(OntologyRelationType(name="owns", source_types=("missing",)),))
    with pytest.raises(ValueError, match="duplicate entity"):
        _schema(entity_types=(OntologyEntityType(name="service"), OntologyEntityType(name="service")))


def test_entity_and_relation_validation_fails_closed_to_quarantine() -> None:
    schema = _schema()
    assert validate_entity(schema, "svc").ok is True
    unknown = validate_entity(schema, "person")
    assert unknown.ok is False
    assert unknown.quarantined is True
    assert validate_relation(schema, "owns", "service", "repository").ok is True
    mismatch = validate_relation(schema, "owns", "repository", "service")
    assert mismatch.reason_code == "ontology_relation_source_mismatch"
    assert mismatch.quarantined is True


def test_registry_artifact_is_sqlite_authoritative_and_proposal_safe() -> None:
    schema = _schema(activation_status="proposal")
    artifact = build_registry_artifact(schema)
    payload = artifact.payload
    assert artifact.artifact_type == "ontology_registry"
    assert payload["authority"] == "sqlite-authoritative"
    assert payload["learned_values_require_review"] is True
    assert payload["schema_digest"] == schema.digest()
    assert "schema" in payload


def test_activation_marker_resolves_declared_schema_and_disables_without_mutation() -> None:
    schema = _schema(activation_status="declared")
    registry = build_registry_artifact(schema).to_record()
    marker = build_activation_artifact(
        schema,
        enabled=True,
        updated_at="2026-08-23T00:00:00Z",
    ).to_record()

    assert resolve_active_schema(
        project=schema.project,
        registry_records=[registry],
        activation_record=marker,
    ).digest() == schema.digest()

    disabled = build_activation_artifact(
        schema,
        enabled=False,
        updated_at="2026-08-23T00:01:00Z",
    ).to_record()
    assert resolve_active_schema(
        project=schema.project,
        registry_records=[registry],
        activation_record=disabled,
    ) is None


def test_activation_marker_fails_closed_on_digest_mismatch() -> None:
    schema = _schema(activation_status="declared")
    registry = build_registry_artifact(schema).to_record()
    marker = build_activation_artifact(
        schema,
        enabled=True,
        updated_at="2026-08-23T00:00:00Z",
    ).to_record()
    marker["schema_digest"] = "a" * 64

    with pytest.raises(OntologyRegistryError, match="digest mismatch"):
        resolve_active_schema(
            project=schema.project,
            registry_records=[registry],
            activation_record=marker,
        )


def test_quarantine_does_not_mutate_schema_or_include_unbounded_content() -> None:
    schema = _schema()
    before = copy.deepcopy(schema.canonical_payload())
    item = quarantine_unknown(schema, kind="entity", value="x" * 1000, reason_code="ontology_entity_unknown")
    assert len(item["value"]) == 160
    assert item["status"] == "quarantined"
    assert schema.canonical_payload() == before


def _relation_write(schema: OntologySchema, **overrides: object) -> OntologyRelationWrite:
    values: dict[str, object] = {
        "project": "blackholememory",
        "schema_digest": schema.digest(),
        "relation": "owns",
        "source_id": "memory-source-private",
        "source_type": "service",
        "target_id": "memory-target-private",
        "target_type": "repository",
    }
    values.update(overrides)
    return OntologyRelationWrite.model_validate(values)


def test_relation_write_admission_is_schema_pinned_content_free_and_non_mutating() -> None:
    schema = _schema()
    receipt = admit_relation_write(schema, _relation_write(schema))

    assert receipt.decision == "allow"
    assert receipt.reason_code == "ontology_relation_allowed"
    assert receipt.source_id_digest != "memory-source-private"
    assert receipt.target_id_digest != "memory-target-private"
    assert receipt.sqlite_mutation is False
    assert receipt.qdrant_mutation is False


def test_relation_write_admission_quarantines_scope_digest_status_and_relation_mismatch() -> None:
    schema = _schema()
    assert admit_relation_write(schema, _relation_write(schema, project="other")).reason_code == "ontology_project_mismatch"
    assert admit_relation_write(schema, _relation_write(schema, schema_digest="a" * 64)).reason_code == "ontology_schema_digest_mismatch"
    assert admit_relation_write(_schema(activation_status="proposal"), _relation_write(_schema(activation_status="proposal"))).reason_code == "ontology_schema_not_declared"
    mismatch = admit_relation_write(schema, _relation_write(schema, source_type="repository", target_type="service"))
    assert mismatch.decision == "quarantine"
    assert mismatch.reason_code == "ontology_relation_source_mismatch"
