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
RELATION = "depends_on"
MAX_DECLARATIONS = 4_096


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


__all__ = [
    "ARTIFACT_TYPE",
    "MAX_DECLARATIONS",
    "RELATION",
    "SCHEMA_VERSION",
    "TaskDependencyDeclaration",
    "TaskDependencyError",
    "append_task_dependency",
    "build_dependency_artifact",
    "dependency_declarations_by_pair",
    "dependency_from_artifact",
    "load_task_dependencies",
]
