"""SQLite-artifact contract for explicit task dependency declarations.

Legacy task sidecars do not contain dependable dependency data.  This module
therefore accepts only an explicit, provenance-bearing operator declaration;
it never tries to reconstruct edges from titles, timestamps, or task status.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .domain import Artifact

SCHEMA_VERSION = "bhm.task-dependency-ledger.v1"
ARTIFACT_TYPE = "task_dependency_declaration"
COMPLETENESS_SCHEMA_VERSION = "bhm.task-dependency-completeness.v1"
COMPLETENESS_ARTIFACT_TYPE = "task_dependency_completeness_attestation"
RELATION = "depends_on"
MAX_DECLARATIONS = 4_096
MAX_TASK_SCOPE = 2_048


class TaskDependencyError(ValueError):
    """Raised when an explicit dependency cannot be safely accepted."""


def _text(value: Any, field_name: str, *, maximum: int = 180) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise TaskDependencyError(f"{field_name} must not be empty")
    if len(normalized) > maximum:
        raise TaskDependencyError(f"{field_name} exceeds {maximum} characters")
    return normalized


def _timestamp(value: Any, field_name: str) -> str:
    raw = _text(value, field_name, maximum=64)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskDependencyError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name, maximum=64).lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise TaskDependencyError(f"{field_name} must be a SHA-256 hex digest")
    return normalized


class TaskDependencyDeclaration(BaseModel):
    """One immutable, same-project ``task_id depends_on dependency`` record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project: str
    task_id: str
    depends_on_task_id: str
    declared_by: str
    declared_at: str
    source_kind: str = "operator_declaration"
    relation: str = RELATION

    @field_validator("project", "task_id", "depends_on_task_id", "declared_by", "source_kind", mode="before")
    @classmethod
    def _required_text(cls, value: Any, info: Any) -> str:
        maximum = 120 if info.field_name in {"project", "declared_by", "source_kind"} else 180
        return _text(value, f"dependency.{info.field_name}", maximum=maximum)

    @field_validator("declared_at", mode="before")
    @classmethod
    def _declared_time(cls, value: Any) -> str:
        return _timestamp(value, "dependency.declared_at")

    @field_validator("relation", mode="before")
    @classmethod
    def _relation(cls, value: Any) -> str:
        if str(value or "").strip() != RELATION:
            raise TaskDependencyError(f"dependency.relation must be {RELATION}")
        return RELATION

    @model_validator(mode="after")
    def _not_self_dependent(self) -> "TaskDependencyDeclaration":
        if self.task_id == self.depends_on_task_id:
            raise TaskDependencyError("task cannot depend on itself")
        return self

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class TaskDependencyCompletenessAttestation(BaseModel):
    """Immutable owner decision that an exact task snapshot has no other edges.

    It does not infer relationships from task content.  Instead, its declared
    edge digest set is the entire allowed dependency set for one exact source
    snapshot.  A source, task scope, or declaration-set drift therefore needs
    a new attestation before a graph may claim completeness.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project: str
    task_ids: tuple[str, ...]
    task_count: int
    task_scope_digest: str
    source_sha256: str
    dependency_declaration_digests: tuple[str, ...] = ()
    dependency_declarations_digest: str
    declared_by: str
    declared_at: str
    source_kind: str = "owner_authorized_complete_scope"
    assertion: str = "all_dependencies_declared"

    @field_validator("project", "declared_by", "source_kind", mode="before")
    @classmethod
    def _required_text(cls, value: Any, info: Any) -> str:
        return _text(value, f"completeness.{info.field_name}", maximum=120)

    @field_validator("declared_at", mode="before")
    @classmethod
    def _declared_time(cls, value: Any) -> str:
        return _timestamp(value, "completeness.declared_at")

    @field_validator("task_ids", mode="before")
    @classmethod
    def _task_ids(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise TaskDependencyError("completeness.task_ids must be an array")
        if not 1 <= len(value) <= MAX_TASK_SCOPE:
            raise TaskDependencyError("completeness.task_ids bound exceeded")
        return tuple(_text(item, "completeness.task_id", maximum=180) for item in value)

    @field_validator("source_sha256", "task_scope_digest", "dependency_declarations_digest", mode="before")
    @classmethod
    def _digests(cls, value: Any, info: Any) -> str:
        return _sha256(value, f"completeness.{info.field_name}")

    @field_validator("dependency_declaration_digests", mode="before")
    @classmethod
    def _declaration_digests(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise TaskDependencyError("completeness.dependency_declaration_digests must be an array")
        if len(value) > MAX_DECLARATIONS:
            raise TaskDependencyError("completeness.dependency_declaration_digests bound exceeded")
        return tuple(_sha256(item, "completeness.dependency_declaration_digest") for item in value)

    @field_validator("assertion", mode="before")
    @classmethod
    def _assertion(cls, value: Any) -> str:
        if str(value or "").strip() != "all_dependencies_declared":
            raise TaskDependencyError("completeness.assertion must be all_dependencies_declared")
        return "all_dependencies_declared"

    @model_validator(mode="after")
    def _validate_aggregate(self) -> "TaskDependencyCompletenessAttestation":
        ordered_ids = tuple(sorted(self.task_ids))
        if self.task_ids != ordered_ids or len(set(self.task_ids)) != len(self.task_ids):
            raise TaskDependencyError("completeness.task_ids must be unique and sorted")
        if self.task_count != len(self.task_ids):
            raise TaskDependencyError("completeness.task_count mismatch")
        if self.task_scope_digest != _task_scope_digest(self.project, self.task_ids):
            raise TaskDependencyError("completeness.task_scope_digest mismatch")
        ordered_declarations = tuple(sorted(self.dependency_declaration_digests))
        if self.dependency_declaration_digests != ordered_declarations or len(set(ordered_declarations)) != len(ordered_declarations):
            raise TaskDependencyError("completeness.dependency_declaration_digests must be unique and sorted")
        if self.dependency_declarations_digest != _digest(list(ordered_declarations)):
            raise TaskDependencyError("completeness.dependency_declarations_digest mismatch")
        return self

    @classmethod
    def from_tasks(
        cls,
        *,
        project: str,
        tasks: Sequence[Mapping[str, Any]],
        source_sha256: str,
        declarations: Sequence[TaskDependencyDeclaration],
        declared_by: str,
        declared_at: str,
    ) -> "TaskDependencyCompletenessAttestation":
        task_ids = tuple(sorted(_known_task_ids(tasks, project=project)))
        declaration_digests = tuple(sorted(declaration.digest() for declaration in declarations))
        return cls(
            project=project,
            task_ids=task_ids,
            task_count=len(task_ids),
            task_scope_digest=_task_scope_digest(project, task_ids),
            source_sha256=source_sha256,
            dependency_declaration_digests=declaration_digests,
            dependency_declarations_digest=_digest(list(declaration_digests)),
            declared_by=declared_by,
            declared_at=declared_at,
        )

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


def build_dependency_artifact(declaration: TaskDependencyDeclaration) -> Artifact:
    """Encode a declaration with immutable identity and replay-safe payload."""

    declaration_digest = declaration.digest()
    return Artifact(
        id=f"task_dependency_{declaration_digest}",
        artifact_type=ARTIFACT_TYPE,
        project=declaration.project,
        created_at=declaration.declared_at,
        updated_at=declaration.declared_at,
        payload={
            "schema_version": SCHEMA_VERSION,
            "declaration": declaration.model_dump(mode="json"),
            "declaration_digest": declaration_digest,
        },
    )


def build_dependency_completeness_artifact(attestation: TaskDependencyCompletenessAttestation) -> Artifact:
    """Encode an immutable exact-snapshot completeness decision."""

    attestation_digest = attestation.digest()
    return Artifact(
        id=f"task_dependency_completeness_{attestation_digest}",
        artifact_type=COMPLETENESS_ARTIFACT_TYPE,
        project=attestation.project,
        created_at=attestation.declared_at,
        updated_at=attestation.declared_at,
        payload={
            "schema_version": COMPLETENESS_SCHEMA_VERSION,
            "attestation": attestation.model_dump(mode="json"),
            "attestation_digest": attestation_digest,
        },
    )


def dependency_from_artifact(record: Mapping[str, Any], *, project: str) -> TaskDependencyDeclaration:
    """Read and validate one project-scoped immutable ledger artifact."""

    if str(record.get("project") or "") != project:
        raise TaskDependencyError("dependency artifact project mismatch")
    if str(record.get("schema_version") or "") != SCHEMA_VERSION:
        raise TaskDependencyError("dependency artifact schema mismatch")
    payload = record.get("declaration")
    if not isinstance(payload, Mapping):
        raise TaskDependencyError("dependency artifact payload is invalid")
    declaration = TaskDependencyDeclaration.model_validate(payload)
    if declaration.project != project:
        raise TaskDependencyError("dependency declaration project mismatch")
    if str(record.get("declaration_digest") or "") != declaration.digest():
        raise TaskDependencyError("dependency artifact digest mismatch")
    return declaration


def dependency_completeness_from_artifact(
    record: Mapping[str, Any], *, project: str
) -> TaskDependencyCompletenessAttestation:
    """Read one immutable attestation without widening its project scope."""

    if str(record.get("project") or "") != project:
        raise TaskDependencyError("completeness artifact project mismatch")
    if str(record.get("schema_version") or "") != COMPLETENESS_SCHEMA_VERSION:
        raise TaskDependencyError("completeness artifact schema mismatch")
    payload = record.get("attestation")
    if not isinstance(payload, Mapping):
        raise TaskDependencyError("completeness artifact payload is invalid")
    attestation = TaskDependencyCompletenessAttestation.model_validate(payload)
    if attestation.project != project:
        raise TaskDependencyError("completeness attestation project mismatch")
    if str(record.get("attestation_digest") or "") != attestation.digest():
        raise TaskDependencyError("completeness artifact digest mismatch")
    return attestation


def _known_task_ids(tasks: Sequence[Mapping[str, Any]], *, project: str) -> set[str]:
    known: set[str] = set()
    for raw in tasks:
        if not isinstance(raw, Mapping):
            raise TaskDependencyError("task source contains a non-object record")
        if str(raw.get("project") or project) != project:
            continue
        task_id = str(raw.get("task_id") or raw.get("id") or "").strip()
        if not task_id:
            raise TaskDependencyError("task source contains a task without identity")
        if task_id in known:
            raise TaskDependencyError("task source contains duplicate identity")
        known.add(task_id)
    return known


def _task_scope_digest(project: str, task_ids: Sequence[str]) -> str:
    return _digest({"project": project, "task_ids": list(task_ids)})


def dependency_declarations_by_pair(
    records: Sequence[Mapping[str, Any] | TaskDependencyDeclaration],
    *,
    project: str,
    known_task_ids: set[str],
) -> dict[tuple[str, str], TaskDependencyDeclaration]:
    """Validate ledger rows and reject unknown endpoints, ambiguity, and cycles."""

    if len(records) > MAX_DECLARATIONS:
        raise TaskDependencyError("dependency declaration bound exceeded")
    declarations: dict[tuple[str, str], TaskDependencyDeclaration] = {}
    for raw in records:
        declaration = raw if isinstance(raw, TaskDependencyDeclaration) else dependency_from_artifact(raw, project=project)
        if declaration.project != project:
            raise TaskDependencyError("dependency declaration project mismatch")
        if declaration.task_id not in known_task_ids or declaration.depends_on_task_id not in known_task_ids:
            raise TaskDependencyError("dependency declaration has an unknown task endpoint")
        pair = (declaration.task_id, declaration.depends_on_task_id)
        previous = declarations.get(pair)
        if previous is not None and previous.digest() != declaration.digest():
            raise TaskDependencyError("dependency declaration is ambiguous")
        declarations[pair] = declaration

    adjacency: dict[str, set[str]] = {}
    for task_id, dependency_id in declarations:
        adjacency.setdefault(task_id, set()).add(dependency_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise TaskDependencyError("dependency declaration introduces a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency_id in sorted(adjacency.get(task_id, ())):
            visit(dependency_id)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in sorted(adjacency):
        visit(task_id)
    return declarations


def append_task_dependency(
    service: Any,
    declaration: TaskDependencyDeclaration,
    *,
    tasks: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Append one declaration after validating it against a bounded task source."""

    known_task_ids = _known_task_ids(tasks, project=declaration.project)
    existing = service.list_artifact_records(
        artifact_type=ARTIFACT_TYPE,
        project=declaration.project,
        limit=MAX_DECLARATIONS,
    )
    dependency_declarations_by_pair(
        [*existing, declaration],
        project=declaration.project,
        known_task_ids=known_task_ids,
    )
    return service.append_artifact(build_dependency_artifact(declaration))


def validate_dependency_completeness_attestation(
    attestation: TaskDependencyCompletenessAttestation,
    *,
    project: str,
    tasks: Sequence[Mapping[str, Any]],
    source_sha256: str,
    declarations: Mapping[tuple[str, str], TaskDependencyDeclaration] | Sequence[TaskDependencyDeclaration],
) -> TaskDependencyCompletenessAttestation:
    """Fail closed unless exact source, task set, and declared edge set match.

    The attestation's edge digest set is deliberately exact.  It therefore
    represents an owner-authorized decision that no additional dependency
    edges exist for this snapshot, without deriving anything from sidecars.
    """

    if attestation.project != project:
        raise TaskDependencyError("completeness attestation project mismatch")
    task_ids = tuple(sorted(_known_task_ids(tasks, project=project)))
    if attestation.task_ids != task_ids or attestation.task_scope_digest != _task_scope_digest(project, task_ids):
        raise TaskDependencyError("completeness attestation task scope mismatch")
    if attestation.source_sha256 != _sha256(source_sha256, "source_sha256"):
        raise TaskDependencyError("completeness attestation source digest mismatch")
    values = list(declarations.values() if isinstance(declarations, Mapping) else declarations)
    known_task_ids = set(task_ids)
    for declaration in values:
        if (
            declaration.project != project
            or declaration.task_id not in known_task_ids
            or declaration.depends_on_task_id not in known_task_ids
        ):
            raise TaskDependencyError("completeness attestation has an edge outside task scope")
    declaration_digests = tuple(sorted(declaration.digest() for declaration in values))
    if attestation.dependency_declaration_digests != declaration_digests:
        raise TaskDependencyError("completeness attestation declared edges mismatch")
    return attestation


def append_task_dependency_completeness_attestation(
    service: Any,
    attestation: TaskDependencyCompletenessAttestation,
    *,
    tasks: Sequence[Mapping[str, Any]],
    source_sha256: str,
) -> tuple[dict[str, Any], bool]:
    """Append one owner decision, rejecting conflicting exact-scope records."""

    declarations = load_task_dependencies(service, project=attestation.project, tasks=tasks)
    validate_dependency_completeness_attestation(
        attestation,
        project=attestation.project,
        tasks=tasks,
        source_sha256=source_sha256,
        declarations=declarations,
    )
    records = service.list_artifact_records(
        artifact_type=COMPLETENESS_ARTIFACT_TYPE,
        project=attestation.project,
        limit=MAX_DECLARATIONS,
    )
    for record in records:
        existing = dependency_completeness_from_artifact(record, project=attestation.project)
        same_scope = existing.task_scope_digest == attestation.task_scope_digest
        same_source = existing.source_sha256 == attestation.source_sha256
        if same_scope and same_source and existing.digest() != attestation.digest():
            raise TaskDependencyError("completeness attestation conflicts with existing exact scope")
    return service.append_artifact(build_dependency_completeness_artifact(attestation))


def load_task_dependencies(
    service: Any,
    *,
    project: str,
    tasks: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], TaskDependencyDeclaration]:
    """Load the complete bounded project ledger; no sidecar inference occurs."""

    known_task_ids = _known_task_ids(tasks, project=project)
    records = service.list_artifact_records(
        artifact_type=ARTIFACT_TYPE,
        project=project,
        limit=MAX_DECLARATIONS,
    )
    return dependency_declarations_by_pair(records, project=project, known_task_ids=known_task_ids)


def load_task_dependency_completeness_attestation(
    service: Any,
    *,
    project: str,
    tasks: Sequence[Mapping[str, Any]],
    source_sha256: str,
    declarations: Mapping[tuple[str, str], TaskDependencyDeclaration] | Sequence[TaskDependencyDeclaration],
) -> TaskDependencyCompletenessAttestation | None:
    """Return exactly one valid attestation for this exact source, or none.

    Historic attestations remain immutable audit records but cannot satisfy a
    changed source.  More than one matching live candidate is ambiguous and
    rejected instead of choosing by timestamp.
    """

    records = service.list_artifact_records(
        artifact_type=COMPLETENESS_ARTIFACT_TYPE,
        project=project,
        limit=MAX_DECLARATIONS,
    )
    current_task_ids = tuple(sorted(_known_task_ids(tasks, project=project)))
    current_scope_digest = _task_scope_digest(project, current_task_ids)
    current_source_sha256 = _sha256(source_sha256, "source_sha256")
    matches: list[TaskDependencyCompletenessAttestation] = []
    for record in records:
        attestation = dependency_completeness_from_artifact(record, project=project)
        exact_scope = (
            attestation.task_scope_digest == current_scope_digest
            and attestation.source_sha256 == current_source_sha256
        )
        try:
            validate_dependency_completeness_attestation(
                attestation,
                project=project,
                tasks=tasks,
                source_sha256=source_sha256,
                declarations=declarations,
            )
        except TaskDependencyError:
            if exact_scope:
                raise
            continue
        matches.append(attestation)
    if len(matches) > 1:
        raise TaskDependencyError("completeness attestation duplicate exact scope")
    return matches[0] if matches else None


__all__ = [
    "ARTIFACT_TYPE",
    "COMPLETENESS_ARTIFACT_TYPE",
    "COMPLETENESS_SCHEMA_VERSION",
    "MAX_DECLARATIONS",
    "MAX_TASK_SCOPE",
    "RELATION",
    "SCHEMA_VERSION",
    "TaskDependencyDeclaration",
    "TaskDependencyCompletenessAttestation",
    "TaskDependencyError",
    "append_task_dependency",
    "append_task_dependency_completeness_attestation",
    "build_dependency_artifact",
    "build_dependency_completeness_artifact",
    "dependency_declarations_by_pair",
    "dependency_from_artifact",
    "dependency_completeness_from_artifact",
    "load_task_dependencies",
    "load_task_dependency_completeness_attestation",
    "validate_dependency_completeness_attestation",
]
