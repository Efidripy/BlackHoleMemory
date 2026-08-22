"""Versioned, proposal-safe project ontology registry for WL-300.3.

The registry is a deterministic contract, not an inference engine.  A schema
may be declared or proposed and can be serialized as a SQLite-authoritative
artifact; learned values are quarantined until an operator promotes a schema
revision.  No graph/database dependency is introduced.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import Artifact


SCHEMA_VERSION = "bhm.ontology-registry.v1"
ARTIFACT_TYPE = "ontology_registry"
ACTIVATION_ARTIFACT_TYPE = "ontology_registry_activation"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,79}$")
ACTIVATION_STATES = frozenset({"declared", "proposal", "active", "deprecated"})
CARDINALITIES = frozenset({"one", "many"})


class OntologyRegistryError(ValueError):
    """Raised when a schema or validation request is unsafe or ambiguous."""


def _name(value: Any, field_name: str) -> str:
    text = str(value or "").strip().casefold()
    if not NAME_PATTERN.fullmatch(text):
        raise ValueError(f"{field_name} must match {NAME_PATTERN.pattern}")
    return text


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"{field_name} must be an array of strings")
    result = tuple(sorted({_name(item, field_name) for item in value}))
    return result


class OntologyEntityType(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    aliases: tuple[str, ...] = ()
    attributes: dict[str, str] = Field(default_factory=dict)
    deprecated: bool = False

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: Any) -> str:
        return _name(value, "entity_type.name")

    @field_validator("aliases", mode="before")
    @classmethod
    def _normalize_aliases(cls, value: Any) -> tuple[str, ...]:
        return _string_tuple(value, "entity_type.aliases")

    @field_validator("attributes", mode="before")
    @classmethod
    def _normalize_attributes(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("entity_type.attributes must be an object")
        normalized: dict[str, str] = {}
        for key, kind in value.items():
            normalized[_name(key, "entity_type.attribute")] = str(kind or "string").strip().casefold()
        return dict(sorted(normalized.items()))


class OntologyRelationType(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    source_types: tuple[str, ...] = ()
    target_types: tuple[str, ...] = ()
    cardinality: Literal["one", "many"] = "many"
    deprecated: bool = False

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: Any) -> str:
        return _name(value, "relation_type.name")

    @field_validator("source_types", "target_types", mode="before")
    @classmethod
    def _normalize_types(cls, value: Any, info: Any) -> tuple[str, ...]:
        return _string_tuple(value, f"relation_type.{info.field_name}")


class OntologySchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project: str = Field(min_length=1, max_length=160)
    schema_version: str = SCHEMA_VERSION
    revision: int = Field(default=1, ge=1, le=1_000_000)
    owner: str = Field(min_length=1, max_length=160)
    activation_status: Literal["declared", "proposal", "active", "deprecated"] = "proposal"
    entity_types: tuple[OntologyEntityType, ...] = ()
    relation_types: tuple[OntologyRelationType, ...] = ()
    provenance: dict[str, str] = Field(default_factory=dict)

    @field_validator("project", "owner", mode="before")
    @classmethod
    def _required_text(cls, value: Any, info: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"ontology.{info.field_name} must not be empty")
        return text

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version(cls, value: Any) -> str:
        value = str(value or "").strip()
        if value != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
        return value

    @field_validator("provenance", mode="before")
    @classmethod
    def _provenance(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("ontology.provenance must be an object")
        return {str(key): str(item) for key, item in sorted(value.items())}

    @model_validator(mode="after")
    def _validate_unique_names(self) -> "OntologySchema":
        entity_names = [item.name for item in self.entity_types]
        relation_names = [item.name for item in self.relation_types]
        if len(set(entity_names)) != len(entity_names):
            raise OntologyRegistryError("duplicate entity type")
        if len(set(relation_names)) != len(relation_names):
            raise OntologyRegistryError("duplicate relation type")
        known_entities = set(entity_names)
        for relation in self.relation_types:
            unknown = (set(relation.source_types) | set(relation.target_types)) - known_entities
            if unknown:
                raise OntologyRegistryError(
                    f"relation {relation.name} references unknown entity type(s): {sorted(unknown)}"
                )
        return self

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["entity_types"] = sorted(payload.get("entity_types") or [], key=lambda item: item["name"])
        payload["relation_types"] = sorted(payload.get("relation_types") or [], key=lambda item: item["name"])
        return payload

    def digest(self) -> str:
        encoded = json.dumps(self.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def entity(self, name: str) -> OntologyEntityType | None:
        candidate = str(name or "").strip().casefold()
        return next((item for item in self.entity_types if candidate == item.name or candidate in item.aliases), None)

    def relation(self, name: str) -> OntologyRelationType | None:
        candidate = str(name or "").strip().casefold()
        return next((item for item in self.relation_types if candidate == item.name), None)


class OntologyValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    kind: Literal["entity", "relation"]
    reason_code: str
    quarantined: bool
    schema_digest: str
    canonical_type: str | None = None
    source_type: str | None = None
    target_type: str | None = None


class OntologyRelationWrite(BaseModel):
    """Content-free intent envelope for a relation write before storage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project: str = Field(min_length=1, max_length=160)
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation: str
    source_id: str = Field(min_length=1, max_length=256)
    source_type: str
    target_id: str = Field(min_length=1, max_length=256)
    target_type: str

    @field_validator("project", mode="before")
    @classmethod
    def _project(cls, value: Any) -> str:
        return _name(value, "relation_write.project")

    @field_validator("relation", "source_type", "target_type", mode="before")
    @classmethod
    def _ontology_name(cls, value: Any, info: Any) -> str:
        return _name(value, f"relation_write.{info.field_name}")


class OntologyRelationAdmissionReceipt(BaseModel):
    """Deterministic allow/quarantine receipt; it never performs a write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SCHEMA_VERSION
    project: str
    schema_digest: str
    decision: Literal["allow", "quarantine"]
    reason_code: str
    relation: str
    source_type: str | None = None
    target_type: str | None = None
    source_id_digest: str
    target_id_digest: str
    content_free: bool = True
    sqlite_mutation: bool = False
    qdrant_mutation: bool = False


def validate_entity(schema: OntologySchema, entity_type: str) -> OntologyValidationResult:
    entity = schema.entity(entity_type)
    if entity is None or entity.deprecated:
        return OntologyValidationResult(
            ok=False,
            kind="entity",
            reason_code="ontology_entity_unknown" if entity is None else "ontology_entity_deprecated",
            quarantined=True,
            schema_digest=schema.digest(),
        )
    return OntologyValidationResult(
        ok=True,
        kind="entity",
        reason_code="ontology_entity_allowed",
        quarantined=False,
        schema_digest=schema.digest(),
        canonical_type=entity.name,
    )


def validate_relation(schema: OntologySchema, relation: str, source_type: str, target_type: str) -> OntologyValidationResult:
    relation_spec = schema.relation(relation)
    source = schema.entity(source_type)
    target = schema.entity(target_type)
    reason = "ontology_relation_allowed"
    ok = True
    if relation_spec is None:
        reason, ok = "ontology_relation_unknown", False
    elif relation_spec.deprecated:
        reason, ok = "ontology_relation_deprecated", False
    elif source is None or target is None:
        reason, ok = "ontology_relation_entity_unknown", False
    elif relation_spec.source_types and source.name not in relation_spec.source_types:
        reason, ok = "ontology_relation_source_mismatch", False
    elif relation_spec.target_types and target.name not in relation_spec.target_types:
        reason, ok = "ontology_relation_target_mismatch", False
    return OntologyValidationResult(
        ok=ok,
        kind="relation",
        reason_code=reason,
        quarantined=not ok,
        schema_digest=schema.digest(),
        canonical_type=relation_spec.name if relation_spec else None,
        source_type=source.name if source else None,
        target_type=target.name if target else None,
    )


def admit_relation_write(
    schema: OntologySchema,
    relation_write: OntologyRelationWrite,
) -> OntologyRelationAdmissionReceipt:
    """Validate a relation intent for disposable adapters, not live storage.

    The caller must bind the request to the exact schema revision.  Proposals
    never become implicit write authority, and the receipt retains only hashed
    endpoint identities so it can be safely logged before an eventual route is
    explicitly activated.
    """

    source_id_digest = hashlib.sha256(relation_write.source_id.encode("utf-8")).hexdigest()
    target_id_digest = hashlib.sha256(relation_write.target_id.encode("utf-8")).hexdigest()
    schema_digest = schema.digest()
    validation: OntologyValidationResult | None = None
    if relation_write.project != schema.project:
        reason_code = "ontology_project_mismatch"
    elif relation_write.schema_digest != schema_digest:
        reason_code = "ontology_schema_digest_mismatch"
    elif schema.activation_status not in {"declared", "active"}:
        reason_code = "ontology_schema_not_declared"
    else:
        validation = validate_relation(
            schema,
            relation_write.relation,
            relation_write.source_type,
            relation_write.target_type,
        )
        reason_code = validation.reason_code
    return OntologyRelationAdmissionReceipt(
        project=relation_write.project,
        schema_digest=schema_digest,
        decision="allow" if validation is not None and validation.ok else "quarantine",
        reason_code=reason_code,
        relation=relation_write.relation,
        source_type=validation.source_type if validation is not None else None,
        target_type=validation.target_type if validation is not None else None,
        source_id_digest=source_id_digest,
        target_id_digest=target_id_digest,
    )


def build_registry_artifact(schema: OntologySchema) -> Artifact:
    """Build a SQLite-authoritative artifact; caller decides when to persist it."""

    digest = schema.digest()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "project": schema.project,
        "revision": schema.revision,
        "activation_status": schema.activation_status,
        "owner": schema.owner,
        "schema_digest": digest,
        "schema": schema.canonical_payload(),
        "authority": "sqlite-authoritative",
        "learned_values_require_review": True,
    }
    return Artifact(
        id=f"ontology_{schema.project}_{schema.revision}_{digest[:16]}",
        artifact_type=ARTIFACT_TYPE,
        project=schema.project,
        created_at="1970-01-01T00:00:00Z",
        payload=payload,
    )


def build_activation_artifact(
    schema: OntologySchema,
    *,
    enabled: bool,
    updated_at: str,
) -> Artifact:
    """Build an explicit, reversible pointer to one declared schema revision.

    Schema documents stay immutable/versioned.  Flipping the activation marker
    is therefore a bounded rollback operation rather than a mutation of the
    declared contract itself.
    """

    schema_artifact = build_registry_artifact(schema)
    return Artifact(
        id=f"ontology_activation_{schema.project}",
        artifact_type=ACTIVATION_ARTIFACT_TYPE,
        project=schema.project,
        created_at=updated_at,
        updated_at=updated_at,
        payload={
            "schema_version": SCHEMA_VERSION,
            "project": schema.project,
            "enabled": bool(enabled),
            "registry_artifact_id": schema_artifact.id,
            "schema_digest": schema.digest(),
            "authority": "sqlite-authoritative",
            "activation_requires_explicit_operator_action": True,
        },
    )


def resolve_active_schema(
    *,
    project: str,
    registry_records: list[Mapping[str, Any]],
    activation_record: Mapping[str, Any] | None,
) -> OntologySchema | None:
    """Resolve one active declared schema or fail closed on an invalid marker."""

    if activation_record is None:
        return None
    if str(activation_record.get("project") or "") != project:
        raise OntologyRegistryError("ontology activation project mismatch")
    if not bool(activation_record.get("enabled")):
        return None
    marker_id = str(activation_record.get("registry_artifact_id") or "")
    marker_digest = str(activation_record.get("schema_digest") or "")
    if not marker_id or not re.fullmatch(r"[0-9a-f]{64}", marker_digest):
        raise OntologyRegistryError("ontology activation marker is invalid")
    matches = [record for record in registry_records if str(record.get("id") or "") == marker_id]
    if len(matches) != 1:
        raise OntologyRegistryError("ontology activation registry artifact is unresolved")
    payload = matches[0].get("schema")
    if not isinstance(payload, Mapping):
        raise OntologyRegistryError("ontology registry artifact schema is invalid")
    schema = OntologySchema.model_validate(payload)
    if schema.project != project or schema.digest() != marker_digest:
        raise OntologyRegistryError("ontology activation schema digest mismatch")
    if schema.activation_status not in {"declared", "active"}:
        raise OntologyRegistryError("ontology activation schema is not declared")
    return schema


def quarantine_unknown(schema: OntologySchema, *, kind: Literal["entity", "relation"], value: str, reason_code: str) -> dict[str, Any]:
    """Return bounded quarantine metadata without silently expanding the schema."""

    return {
        "schema_version": SCHEMA_VERSION,
        "project": schema.project,
        "schema_digest": schema.digest(),
        "kind": kind,
        "value": str(value or "")[:160],
        "reason_code": reason_code,
        "status": "quarantined",
        "authority": "sqlite-authoritative",
        "requires_review": True,
    }


__all__ = [
    "ACTIVATION_ARTIFACT_TYPE",
    "ACTIVATION_STATES",
    "ARTIFACT_TYPE",
    "CARDINALITIES",
    "NAME_PATTERN",
    "OntologyEntityType",
    "OntologyRelationAdmissionReceipt",
    "OntologyRegistryError",
    "OntologyRelationType",
    "OntologyRelationWrite",
    "OntologySchema",
    "OntologyValidationResult",
    "SCHEMA_VERSION",
    "admit_relation_write",
    "build_activation_artifact",
    "build_registry_artifact",
    "quarantine_unknown",
    "resolve_active_schema",
    "validate_entity",
    "validate_relation",
]
