"""Fail-closed authority bindings for Qdrant projection generations.

A binding records a reviewed generation as SQLite metadata.  Qdrant remains a
rebuildable projection: failure to read, validate, or revalidate a binding
always resolves to the legacy local collection.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .filesystem_boundaries import assert_safe_path
from .memory_repository import MemoryRepositoryError
from .memory_repository import PROJECTION_GENERATION_BINDING_SCHEMA_VERSION

_BINDING_REQUIRED = frozenset(
    {
        "project",
        "target_collection",
        "plan_digest",
        "source_basis_digest",
        "generation_id",
        "target_set_digest",
        "embedding_model",
        "vector_dimensions",
        "distance",
        "expected_point_count",
        "parity_digest",
    }
)
_GENERATION_COLLECTION_PATTERN = re.compile(r"bhm_projection_generation_[a-z0-9_-]+")

def _generation_source_basis_digest(*args: Any, **kwargs: Any) -> Any:
    from .projection_reconciliation import _generation_source_basis_digest as _impl

    return _impl(*args, **kwargs)

class ProjectionGenerationBindingError(MemoryRepositoryError):
    """A binding or its authority proof is incomplete or changed."""

class ProjectionRouteUnavailable(ProjectionGenerationBindingError):
    """A migrated project has no safe local Qdrant route."""

@dataclass(frozen=True)
class ProjectionCollectionRoute:
    """One explicit local projection route.

    ``collection_name`` is deliberately absent rather than falling back to a
    legacy collection when an already-migrated project loses its binding
    proof.  Callers must surface a bounded unavailable outcome in that case.
    """

    collection_name: str | None
    reason: str
    legacy_allowed: bool

@dataclass(frozen=True)
class _BindingAuthoritySnapshot:
    active: tuple["ProjectionGenerationBinding", ...]
    historical_projects: frozenset[str]
    available: bool
    legacy_schema: bool

def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()

def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_fingerprint(path: Path | str) -> dict[str, object]:
    target = assert_safe_path(Path(path).expanduser()).resolve()
    if not target.is_file():
        raise ProjectionGenerationBindingError("SQLite database or backup is missing")
    connection = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold()
        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    stat = target.stat()
    return {
        "path": str(target),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": _file_sha256(target),
        "wal": None if not Path(f"{target}-wal").exists() else {"size": int(Path(f"{target}-wal").stat().st_size), "sha256": _file_sha256(Path(f"{target}-wal"))},
        "user_version": user_version,
        "quick_check": quick_check,
        "foreign_key_errors": foreign_key_errors,
    }


def _nonempty(value: object, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ProjectionGenerationBindingError(f"binding {name} is required")
    return result


def _digest(value: object, name: str) -> str:
    result = _nonempty(value, name).casefold()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ProjectionGenerationBindingError(f"binding {name} must be a SHA-256 digest")
    return result


def projection_target_set_digest(collection_names: Sequence[str]) -> str:
    """Fingerprint one exact target collection set using the established receipt form.

    Project association is validated separately by the binding plan.  This
    digest deliberately covers only the collection-name set, preserving the
    digest contract used by the historical shadow-generation receipts.
    """

    if isinstance(collection_names, (str, bytes)):
        raise ProjectionGenerationBindingError("target collection set must be an explicit sequence")
    names = tuple(_nonempty(value, "target_collection") for value in collection_names)
    if not names:
        raise ProjectionGenerationBindingError("target collection set is empty")
    if len(set(names)) != len(names):
        raise ProjectionGenerationBindingError("target collection set contains a duplicate")
    if any(not _GENERATION_COLLECTION_PATTERN.fullmatch(name) for name in names):
        raise ProjectionGenerationBindingError("target collection set contains an unsafe generation name")
    return hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProjectionGenerationBinding:
    project: str
    target_collection: str
    plan_digest: str
    source_basis_digest: str
    generation_id: str
    target_set_digest: str
    embedding_model: str
    vector_dimensions: int
    distance: str
    expected_point_count: int
    parity_digest: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProjectionGenerationBinding":
        missing = sorted(_BINDING_REQUIRED - set(value))
        if missing:
            raise ProjectionGenerationBindingError(f"binding fields are missing: {', '.join(missing)}")
        dimensions = int(value["vector_dimensions"])
        point_count = int(value["expected_point_count"])
        if dimensions <= 0 or point_count <= 0:
            raise ProjectionGenerationBindingError("binding dimensions and expected_point_count must be positive")
        binding = cls(
            project=_nonempty(value["project"], "project"),
            target_collection=_nonempty(value["target_collection"], "target_collection"),
            plan_digest=_digest(value["plan_digest"], "plan_digest"),
            source_basis_digest=_digest(value["source_basis_digest"], "source_basis_digest"),
            generation_id=_digest(value["generation_id"], "generation_id"),
            target_set_digest=_digest(value["target_set_digest"], "target_set_digest"),
            embedding_model=_nonempty(value["embedding_model"], "embedding_model"),
            vector_dimensions=dimensions,
            distance=_nonempty(value["distance"], "distance"),
            expected_point_count=point_count,
            parity_digest=_digest(value["parity_digest"], "parity_digest"),
        )
        if binding.target_collection.casefold() == binding.project.casefold():
            raise ProjectionGenerationBindingError("target collection must not alias a project identifier")
        return binding

    @property
    def binding_digest(self) -> str:
        return _sha256(asdict(self))

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "binding_digest": self.binding_digest}


def build_projection_generation_binding_plan(
    bindings: Sequence[Mapping[str, Any] | ProjectionGenerationBinding],
    *,
    database: Path | str,
    existing_backup: Path | str,
    as_of: str,
    actor_digest: str,
    correlation_id: str,
) -> dict[str, object]:
    """Return a non-persisting atomic binding plan for one generation set."""

    if not str(as_of).strip():
        raise ProjectionGenerationBindingError("as_of is required")
    database_path = assert_safe_path(Path(database).expanduser()).resolve()
    backup_path = assert_safe_path(Path(existing_backup).expanduser()).resolve()
    if database_path == backup_path:
        raise ProjectionGenerationBindingError("existing backup must be distinct from authoritative SQLite")
    database_fingerprint = _sqlite_fingerprint(database_path)
    backup_fingerprint = _sqlite_fingerprint(backup_path)
    if database_fingerprint["user_version"] != PROJECTION_GENERATION_BINDING_SCHEMA_VERSION:
        raise ProjectionGenerationBindingError("projection binding schema is unavailable")
    if any(item["quick_check"] != "ok" or item["foreign_key_errors"] for item in (database_fingerprint, backup_fingerprint)):
        raise ProjectionGenerationBindingError("SQLite or backup integrity preflight failed")
    parsed = tuple(
        item if isinstance(item, ProjectionGenerationBinding) else ProjectionGenerationBinding.from_mapping(item)
        for item in bindings
    )
    if not parsed:
        raise ProjectionGenerationBindingError("at least one exact binding is required")
    projects = [item.project for item in parsed]
    collections = [item.target_collection for item in parsed]
    if len(set(projects)) != len(projects):
        raise ProjectionGenerationBindingError("duplicate project binding is rejected")
    if len(set(collections)) != len(collections):
        raise ProjectionGenerationBindingError("target collection collision is rejected")
    expected_target_set_digest = projection_target_set_digest(collections)
    if any(item.target_set_digest != expected_target_set_digest for item in parsed):
        raise ProjectionGenerationBindingError("binding target_set_digest does not match the exact target collection set")
    expected = {
        "plan_digest": {item.plan_digest for item in parsed},
        "source_basis_digest": {item.source_basis_digest for item in parsed},
        "generation_id": {item.generation_id for item in parsed},
        "target_set_digest": {item.target_set_digest for item in parsed},
        "embedding_model": {item.embedding_model for item in parsed},
        "vector_dimensions": {item.vector_dimensions for item in parsed},
        "distance": {item.distance.casefold() for item in parsed},
    }
    inconsistent = sorted(key for key, values in expected.items() if len(values) != 1)
    if inconsistent:
        raise ProjectionGenerationBindingError(f"binding generation set is inconsistent: {', '.join(inconsistent)}")
    ordered = tuple(sorted(parsed, key=lambda item: item.project))
    plan: dict[str, object] = {
        "schema_version": "bhm.projection-generation-binding-plan.v1",
        "as_of": str(as_of),
        "actor_digest": _digest(actor_digest, "actor_digest"),
        "correlation_id": _nonempty(correlation_id, "correlation_id"),
        "database": database_fingerprint,
        "existing_backup": backup_fingerprint,
        "bindings": [item.to_dict() for item in ordered],
        "side_effects": {"read_only": True, "sqlite_written": False, "qdrant_written": False, "mem0_written": False},
    }
    plan["plan_digest"] = _sha256(plan)
    return plan


def _verified_plan(
    plan: Mapping[str, object],
    expected_plan_digest: str,
    database: Path | str,
    existing_backup: Path | str,
    *,
    verify_authority_fingerprint: bool,
) -> tuple[ProjectionGenerationBinding, ...]:
    unsigned = dict(plan)
    supplied = str(unsigned.pop("plan_digest", ""))
    if supplied != expected_plan_digest or _sha256(unsigned) != expected_plan_digest:
        raise ProjectionGenerationBindingError("binding plan digest mismatch")
    raw_bindings = plan.get("bindings")
    if not isinstance(raw_bindings, list):
        raise ProjectionGenerationBindingError("binding plan has no bindings")
    reconstructed = build_projection_generation_binding_plan(
        raw_bindings,
        database=database,
        existing_backup=existing_backup,
        as_of=str(plan.get("as_of") or ""),
        actor_digest=str(plan.get("actor_digest") or ""),
        correlation_id=str(plan.get("correlation_id") or ""),
    )
    for key in ("database", "existing_backup"):
        reconstructed[key] = plan.get(key)
    reconstructed["plan_digest"] = _sha256({key: value for key, value in reconstructed.items() if key != "plan_digest"})
    if reconstructed["plan_digest"] != expected_plan_digest:
        raise ProjectionGenerationBindingError("binding plan canonical form changed")
    if verify_authority_fingerprint:
        for actual, planned, label in (
            (_sqlite_fingerprint(database), plan.get("database"), "authoritative database"),
            (_sqlite_fingerprint(existing_backup), plan.get("existing_backup"), "existing backup"),
        ):
            if not isinstance(planned, Mapping):
                raise ProjectionGenerationBindingError("binding plan lacks authority backup fingerprint")
            for key in ("size", "mtime_ns", "sha256", "wal", "user_version", "quick_check", "foreign_key_errors"):
                if actual.get(key) != planned.get(key):
                    raise ProjectionGenerationBindingError(f"{label} changed since binding plan: {key}")
    return tuple(ProjectionGenerationBinding.from_mapping(item) for item in raw_bindings)


def _connection(database: Path | str, *, read_only: bool) -> sqlite3.Connection:
    target = assert_safe_path(Path(database).expanduser()).resolve()
    if not target.is_file():
        raise ProjectionGenerationBindingError("authoritative SQLite database is missing")
    uri = f"file:{target.as_posix()}?mode={'ro' if read_only else 'rw'}"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    if read_only:
        connection.execute("PRAGMA query_only=ON")
    return connection


def _schema_ready(connection: sqlite3.Connection) -> bool:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != PROJECTION_GENERATION_BINDING_SCHEMA_VERSION:
        return False
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"projection_generation_bindings", "projection_generation_binding_events"}.issubset(tables)


def _event(connection: sqlite3.Connection, *, binding_id: str, action: str, binding: ProjectionGenerationBinding, actor_digest: str, correlation_id: str, occurred_at: str) -> None:
    details = {"binding_digest": binding.binding_digest, "project": binding.project, "target_collection": binding.target_collection}
    event_digest = _sha256({"binding_id": binding_id, "action": action, "details": details, "actor_digest": actor_digest, "correlation_id": correlation_id, "occurred_at": occurred_at})
    connection.execute(
        "INSERT INTO projection_generation_binding_events(event_id, binding_id, action, event_digest, actor_digest, correlation_id, occurred_at, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"pge_{event_digest[:28]}", binding_id, action, event_digest, actor_digest, correlation_id, occurred_at, _canonical_json(details)),
    )


def apply_projection_generation_binding_plan(
    database: Path | str,
    existing_backup: Path | str,
    plan: Mapping[str, object],
    *,
    expected_plan_digest: str,
    confirm_operator: bool = False,
    offline_verified: bool = False,
) -> dict[str, object]:
    """Persist an exact plan atomically; activation is all-or-nothing per generation."""

    if not confirm_operator or not offline_verified:
        raise ProjectionGenerationBindingError("binding apply requires explicit operator confirmation and offline writer proof")
    bindings = _verified_plan(
        plan,
        expected_plan_digest,
        database,
        existing_backup,
        verify_authority_fingerprint=False,
    )
    actor_digest = str(plan["actor_digest"])
    correlation_id = str(plan["correlation_id"])
    connection = _connection(database, read_only=False)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        if not _schema_ready(connection):
            raise ProjectionGenerationBindingError("projection binding schema is unavailable")
        rows = connection.execute(
            "SELECT project, binding_digest FROM projection_generation_bindings WHERE binding_digest IN ({})".format(
                ",".join("?" for _ in bindings)
            ),
            tuple(item.binding_digest for item in bindings),
        ).fetchall()
        if rows:
            if len(rows) != len(bindings) or {str(row["project"]) for row in rows} != {item.project for item in bindings}:
                raise ProjectionGenerationBindingError("idempotency key conflicts with another binding set")
            connection.commit()
            return {"ok": True, "action": "already-applied", "plan_digest": expected_plan_digest, "bindings": len(bindings), "sqlite_written": False}
        _verified_plan(
            plan,
            expected_plan_digest,
            database,
            existing_backup,
            verify_authority_fingerprint=True,
        )
        existing = connection.execute(
            "SELECT project, target_collection FROM projection_generation_bindings WHERE state = 'active' AND (project IN ({}) OR target_collection IN ({}))".format(
                ",".join("?" for _ in bindings), ",".join("?" for _ in bindings)
            ),
            tuple(item.project for item in bindings) + tuple(item.target_collection for item in bindings),
        ).fetchall()
        if existing:
            raise ProjectionGenerationBindingError("active binding conflict; exact rollback is required before replacement")
        now = _now()
        for binding in bindings:
            binding_id = f"pgb_{binding.binding_digest[:28]}"
            connection.execute(
                """
                INSERT INTO projection_generation_bindings(
                    binding_id, binding_digest, project, target_collection, plan_digest, source_basis_digest,
                    generation_id, target_set_digest, embedding_model, vector_dimensions, distance,
                    expected_point_count, parity_digest, state, actor_digest, correlation_id, idempotency_key,
                    supersedes_binding_id, created_at, prepared_at, activated_at, rolled_back_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, ?, ?, ?, NULL)
                """,
                (
                    binding_id, binding.binding_digest, binding.project, binding.target_collection, binding.plan_digest,
                    binding.source_basis_digest, binding.generation_id, binding.target_set_digest, binding.embedding_model,
                    binding.vector_dimensions, binding.distance, binding.expected_point_count, binding.parity_digest,
                    actor_digest, correlation_id, f"{expected_plan_digest}:{binding.binding_digest}", now, now, now,
                ),
            )
            _event(connection, binding_id=binding_id, action="prepared", binding=binding, actor_digest=actor_digest, correlation_id=correlation_id, occurred_at=now)
            _event(connection, binding_id=binding_id, action="activated", binding=binding, actor_digest=actor_digest, correlation_id=correlation_id, occurred_at=now)
        connection.commit()
    except Exception:  # broad: rollback transaction before re-raising to preserve binding failure semantics
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"ok": True, "action": "applied", "plan_digest": expected_plan_digest, "bindings": len(bindings), "sqlite_written": True}


def rollback_projection_generation_binding(
    database: Path | str,
    existing_backup: Path | str,
    *,
    project: str,
    binding_digest: str,
    actor_digest: str,
    correlation_id: str,
    confirm_operator: bool = False,
    offline_verified: bool = False,
) -> dict[str, object]:
    """Revoke one exact active binding and retain its immutable provenance row."""

    if not confirm_operator or not offline_verified:
        raise ProjectionGenerationBindingError("binding rollback requires explicit operator confirmation and offline writer proof")
    database_path = assert_safe_path(Path(database).expanduser()).resolve()
    backup_path = assert_safe_path(Path(existing_backup).expanduser()).resolve()
    if database_path == backup_path:
        raise ProjectionGenerationBindingError("existing backup must be distinct from authoritative SQLite")
    for fingerprint in (_sqlite_fingerprint(database_path), _sqlite_fingerprint(backup_path)):
        if fingerprint["quick_check"] != "ok" or fingerprint["foreign_key_errors"]:
            raise ProjectionGenerationBindingError("SQLite or backup integrity preflight failed")
    canonical_digest = _digest(binding_digest, "binding_digest")
    connection = _connection(database, read_only=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if not _schema_ready(connection):
            raise ProjectionGenerationBindingError("projection binding schema is unavailable")
        row = connection.execute(
            "SELECT * FROM projection_generation_bindings WHERE project = ? AND binding_digest = ?",
            (_nonempty(project, "project"), canonical_digest),
        ).fetchone()
        if row is None:
            raise ProjectionGenerationBindingError("exact binding is absent")
        if str(row["state"]) == "rolled_back":
            connection.commit()
            return {"ok": True, "action": "already-rolled-back", "binding_digest": canonical_digest, "sqlite_written": False}
        if str(row["state"]) != "active":
            raise ProjectionGenerationBindingError("only an active binding can be rolled back")
        binding = ProjectionGenerationBinding.from_mapping(dict(row))
        now = _now()
        connection.execute("UPDATE projection_generation_bindings SET state = 'rolled_back', rolled_back_at = ? WHERE binding_id = ?", (now, str(row["binding_id"])))
        _event(connection, binding_id=str(row["binding_id"]), action="rolled_back", binding=binding, actor_digest=_digest(actor_digest, "actor_digest"), correlation_id=_nonempty(correlation_id, "correlation_id"), occurred_at=now)
        connection.commit()
    except Exception:  # broad: rollback transaction before re-raising to preserve binding failure semantics
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"ok": True, "action": "rolled-back", "binding_digest": canonical_digest, "sqlite_written": True}


def active_projection_generation_bindings(database: Path | str) -> tuple[ProjectionGenerationBinding, ...]:
    """Read active routing metadata only; missing v4 is intentionally empty."""

    try:
        connection = _connection(database, read_only=True)
    except (OSError, sqlite3.Error, ProjectionGenerationBindingError):
        return ()
    try:
        if not _schema_ready(connection):
            return ()
        rows = connection.execute("SELECT * FROM projection_generation_bindings WHERE state = 'active' ORDER BY project").fetchall()
        return tuple(ProjectionGenerationBinding.from_mapping(dict(row)) for row in rows)
    except (sqlite3.Error, ProjectionGenerationBindingError):
        return ()
    finally:
        connection.close()


def _binding_authority_snapshot(database: Path | str) -> _BindingAuthoritySnapshot:
    """Read active and historical binding identity without hiding authority loss."""

    try:
        connection = _connection(database, read_only=True)
    except (OSError, sqlite3.Error, ProjectionGenerationBindingError):
        return _BindingAuthoritySnapshot((), frozenset(), False, False)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < PROJECTION_GENERATION_BINDING_SCHEMA_VERSION:
            return _BindingAuthoritySnapshot((), frozenset(), True, True)
        if not _schema_ready(connection):
            return _BindingAuthoritySnapshot((), frozenset(), False, False)
        rows = tuple(
            ProjectionGenerationBinding.from_mapping(dict(row))
            for row in connection.execute(
                "SELECT * FROM projection_generation_bindings ORDER BY project, created_at"
            ).fetchall()
        )
        return _BindingAuthoritySnapshot(
            tuple(item for item in rows if _binding_state(connection, item) == "active"),
            frozenset(item.project for item in rows),
            True,
            False,
        )
    except (sqlite3.Error, ProjectionGenerationBindingError, ValueError):
        return _BindingAuthoritySnapshot((), frozenset(), False, False)
    finally:
        connection.close()


def _binding_state(connection: sqlite3.Connection, binding: ProjectionGenerationBinding) -> str:
    row = connection.execute(
        "SELECT state FROM projection_generation_bindings WHERE binding_digest = ?",
        (binding.binding_digest,),
    ).fetchone()
    return str(row["state"]) if row is not None else ""


def resolve_projection_route(
    *,
    project: str,
    legacy_collection: str,
    database: Path | str,
    embedding_model: str,
    vector_dimensions: int,
    distance: str,
) -> ProjectionCollectionRoute:
    """Resolve a local collection without reopening retired legacy fallback."""

    snapshot = _binding_authority_snapshot(database)
    if not snapshot.available:
        return ProjectionCollectionRoute(None, "binding_authority_unavailable", False)
    if snapshot.legacy_schema:
        return ProjectionCollectionRoute(legacy_collection, "binding_schema_legacy", True)
    candidate = next((item for item in snapshot.active if item.project == project), None)
    if candidate is None:
        if project in snapshot.historical_projects:
            return ProjectionCollectionRoute(None, "binding_retired_unavailable", False)
        return ProjectionCollectionRoute(legacy_collection, "binding_unavailable", True)
    group = tuple(item for item in snapshot.active if item.generation_id == candidate.generation_id)
    if not group or any(
        item.source_basis_digest != candidate.source_basis_digest
        or item.target_set_digest != candidate.target_set_digest
        or item.plan_digest != candidate.plan_digest
        for item in group
    ):
        return ProjectionCollectionRoute(None, "binding_generation_inconsistent", False)
    if candidate.embedding_model != embedding_model or candidate.vector_dimensions != vector_dimensions or candidate.distance.casefold() != distance.casefold():
        return ProjectionCollectionRoute(None, "binding_vector_space_mismatch", False)
    try:
        from .memory_repository import SQLiteMemoryRepository

        repository = SQLiteMemoryRepository(database)
        memories = []
        for item in group:
            memories.extend(repository.list_memories(project=item.project, include_archived=True, include_tombstoned=True, limit=10_000))



        if _generation_source_basis_digest(memories) != candidate.source_basis_digest:
            return ProjectionCollectionRoute(None, "binding_source_drift", False)
    except Exception:  # broad: return fail-closed route when source inspection is unavailable
        return ProjectionCollectionRoute(None, "binding_source_unavailable", False)
    return ProjectionCollectionRoute(candidate.target_collection, "binding_active", False)


def resolve_bound_collection(
    *,
    project: str,
    legacy_collection: str,
    database: Path | str,
    embedding_model: str,
    vector_dimensions: int,
    distance: str,
) -> tuple[str | None, str]:
    """Compatibility wrapper for consumers that need only route name/reason."""

    route = resolve_projection_route(
        project=project,
        legacy_collection=legacy_collection,
        database=database,
        embedding_model=embedding_model,
        vector_dimensions=vector_dimensions,
        distance=distance,
    )
    return route.collection_name, route.reason


__all__ = [
    "ProjectionGenerationBinding",
    "ProjectionGenerationBindingError",
    "ProjectionCollectionRoute",
    "ProjectionRouteUnavailable",
    "active_projection_generation_bindings",
    "apply_projection_generation_binding_plan",
    "build_projection_generation_binding_plan",
    "projection_target_set_digest",
    "resolve_projection_route",
    "resolve_bound_collection",
    "rollback_projection_generation_binding",
]
