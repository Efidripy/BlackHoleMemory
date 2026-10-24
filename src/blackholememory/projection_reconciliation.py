"""Dry-run-first reconciliation for canonical memories and Qdrant projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Any

from qdrant_client.http import models as qdrant_models

from .domain import Lifecycle
from .memory_repository import MemoryRepository
from .projection_reconciliation_base import (
    QDRANT_GENERATION_COLLECTION_PREFIX,  # noqa: F401
    QDRANT_QUARANTINE_COLLECTION_PREFIX,
    ProjectionApplyResult,
    ProjectionCompatibility,
    ProjectionCompatibilityStatus,
    ProjectionReconciliationPlan,
    ProjectionReviewClassification,
    ProjectionReviewDisposition,
    ProjectionSurface,
    ReconciliationAction,
    ReconciliationEntry,
    _digest_payload,
)
from .projection_reconciliation_generation import (
    ProjectionGenerationApplyResult,  # noqa: F401
    ProjectionGenerationEntry,  # noqa: F401
    ProjectionGenerationPlan,  # noqa: F401
    _generation_collection_name,  # noqa: F401
    _generation_source_basis_digest,  # noqa: F401
    _target_generation_label,  # noqa: F401
    apply_projection_generation_plan,  # noqa: F401
    build_projection_generation_plan,  # noqa: F401
)
from .qdrant_projector import QdrantProjector
from .qdrant_projector import build_generation_point_payload  # noqa: F401
from .qdrant_projector import deterministic_point_id
from .qdrant_projector import projection_payload_digest
from .qdrant_projector import projection_payload_digest_from_payload
from .qdrant_validation_bounds import ValidationScanBudget

_QDRANT_RETRIEVE_BATCH_SIZE = 256


class QdrantSurfaceAdapter:
    """Read/delete adapter around a Qdrant client; writes stay in projector."""

    def __init__(
        self, client: Any, *, validation_budget: ValidationScanBudget | None = None
    ) -> None:
        self.client = client
        self.validation_budget = validation_budget

    def _request(self, stage: str) -> None:
        if self.validation_budget is not None:
            self.validation_budget.request(stage)

    def list_collections(self) -> list[str]:
        self._request("reconciliation.collections")
        return sorted(
            str(item.name)
            for item in self.client.get_collections().collections
            if getattr(item, "name", None)
        )

    def get_point(self, collection_name: str, point_id: str) -> dict[str, Any] | None:
        self._request("reconciliation.retrieve")
        points = self.client.retrieve(
            collection_name=collection_name,
            ids=[point_id],
            with_payload=True,
            with_vectors=False,
        )
        if self.validation_budget is not None:
            self.validation_budget.checkpoint("reconciliation.retrieve")
        if not points:
            return None
        point = points[0]
        return {
            "id": str(point.id),
            "payload": dict(point.payload or {}),
        }

    def get_points(
        self, collection_name: str, point_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Read a bounded group of points without requesting vectors.

        A full authority-to-projection validation may contain thousands of
        SQLite memories.  Fetching each point separately makes the read-only
        validator O(N) remote round trips and causes a false incomplete result
        before any lifecycle assertion can be evaluated.  This keeps the same
        exact point identity checks while bounding each Qdrant request to 256
        IDs and preserving a per-request budget receipt.
        """

        unique_ids = list(dict.fromkeys(str(item) for item in point_ids if str(item)))
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique_ids), _QDRANT_RETRIEVE_BATCH_SIZE):
            batch = unique_ids[start : start + _QDRANT_RETRIEVE_BATCH_SIZE]
            self._request("reconciliation.retrieve_batch")
            points = self.client.retrieve(
                collection_name=collection_name,
                ids=batch,
                with_payload=True,
                with_vectors=False,
            )
            if self.validation_budget is not None:
                self.validation_budget.checkpoint("reconciliation.retrieve_batch")
            for point in points:
                result[str(point.id)] = {
                    "id": str(point.id),
                    "payload": dict(point.payload or {}),
                }
        return result

    def list_points(self, collection_name: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset: Any = None
        while True:
            if self.validation_budget is not None:
                self.validation_budget.page("reconciliation.scroll")
            self._request("reconciliation.scroll")
            points, offset = self.client.scroll(
                collection_name=collection_name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            if self.validation_budget is not None:
                self.validation_budget.checkpoint("reconciliation.scroll")
            result.extend(
                {"id": str(point.id), "payload": dict(point.payload or {})}
                for point in points
            )
            if offset is None or not points:
                return result

    def delete_point(self, collection_name: str, point_id: str) -> None:
        self.client.delete(
            collection_name=collection_name,
            points_selector=qdrant_models.PointIdsList(points=[point_id]),
            wait=True,
        )

    def collection_vector_dimensions(self, collection_name: str) -> int | None:
        exists = getattr(self.client, "collection_exists", None)
        if callable(exists):
            self._request("reconciliation.collection_exists")
        if callable(exists):
            if not exists(collection_name):
                return None
            if self.validation_budget is not None:
                self.validation_budget.checkpoint("reconciliation.collection_exists")
        if not callable(exists) and collection_name not in self.list_collections():
            return None
        get_collection = getattr(self.client, "get_collection", None)
        if not callable(get_collection):
            # Compatibility remains available to older read-only surfaces;
            # callers that ask for an expected dimension receive an explicit
            # mismatch rather than a guessed collection configuration.
            return None
        self._request("reconciliation.get_collection")
        details = get_collection(collection_name=collection_name)
        if self.validation_budget is not None:
            self.validation_budget.checkpoint("reconciliation.get_collection")
        vectors = getattr(getattr(details, "config", None), "params", None)
        vectors = getattr(vectors, "vectors", None)
        if isinstance(vectors, Mapping):
            # BHM writes one unnamed dense vector.  A named-vector collection
            # is not silently guessed as compatible.
            return None
        size = getattr(vectors, "size", None)
        return int(size) if isinstance(size, int) else None


def _payload_digest(payload: Mapping[str, Any] | None) -> str:
    """Fingerprint an observed projection for apply-time TOCTOU checks."""

    canonical = json.dumps(
        dict(payload or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _revalidate_review_delete(
    plan: ProjectionReconciliationPlan,
    entry: ReconciliationEntry,
    surface: ProjectionSurface,
) -> None:
    """Re-read an orphan before operator-approved destructive cleanup."""

    observed = surface.get_point(entry.collection_name, entry.point_id)
    if not isinstance(observed, Mapping):
        raise RuntimeError("projection disappeared or could not be revalidated")
    payload = (
        observed.get("payload") if isinstance(observed.get("payload"), Mapping) else {}
    )
    if _payload_digest(payload) != _payload_digest(entry.observed_payload):
        raise RuntimeError("projection changed after reconciliation plan")
    if plan.project is not None and str(payload.get("project") or "") != str(
        plan.project
    ):
        raise RuntimeError("projection crossed project boundary")


def _revalidate_tombstone_delete(
    plan: ProjectionReconciliationPlan,
    entry: ReconciliationEntry,
    memory: Any,
    surface: ProjectionSurface,
) -> None:
    """Re-read canonical tombstone projection and authoritative SQLite state."""

    if getattr(memory, "lifecycle", None) is not Lifecycle.TOMBSTONED:
        raise RuntimeError("authoritative memory is no longer tombstoned")
    if plan.project is not None and str(getattr(memory, "project", "")) != str(
        plan.project
    ):
        raise RuntimeError("authoritative memory crossed project boundary")
    observed = surface.get_point(entry.collection_name, entry.point_id)
    if not isinstance(observed, Mapping):
        raise RuntimeError(
            "tombstone projection disappeared or could not be revalidated"
        )
    payload = (
        observed.get("payload") if isinstance(observed.get("payload"), Mapping) else {}
    )
    if str(payload.get("source_id") or "") != str(entry.memory_id or ""):
        raise RuntimeError("tombstone projection source changed after plan")
    if str(payload.get("revision_id") or "") != str(entry.observed_revision_id or ""):
        raise RuntimeError("tombstone projection revision changed after plan")
    expected_lifecycle = str((entry.observed_payload or {}).get("lifecycle") or "")
    if str(payload.get("lifecycle") or "") != expected_lifecycle:
        raise RuntimeError("tombstone projection lifecycle changed after plan")
    if plan.project is not None and str(payload.get("project") or "") != str(
        plan.project
    ):
        raise RuntimeError("tombstone projection crossed project boundary")


def classify_projection_review_entries(
    plan: ProjectionReconciliationPlan,
    *,
    known_memory_ids: set[str],
) -> tuple[ProjectionReviewClassification, ...]:
    """Classify REVIEW entries without mutating SQLite or Qdrant.

    A known source id is a duplicate candidate only when every desired
    canonical point for that memory is already a NOOP. Unknown source ids are
    retained for manual review; known ids with a missing/stale desired point
    must be repaired before any orphan cleanup is considered.
    """

    desired_by_memory: dict[str, list[ReconciliationEntry]] = {}
    canonical_collections: set[str] = set()
    for entry in plan.entries:
        if entry.action is ReconciliationAction.REVIEW:
            continue
        canonical_collections.add(entry.collection_name)
        if entry.memory_id is not None:
            desired_by_memory.setdefault(entry.memory_id, []).append(entry)

    classifications: list[ProjectionReviewClassification] = []
    for entry in plan.entries:
        if entry.action is not ReconciliationAction.REVIEW:
            continue
        payload = entry.observed_payload or {}
        source_id = entry.memory_id or str(payload.get("source_id") or "") or None
        if entry.collection_name in canonical_collections:
            surface = "canonical_named"
        else:
            surface = "noncanonical_named"

        desired = desired_by_memory.get(source_id or "", [])
        if source_id is None or source_id not in known_memory_ids:
            source_state = "unknown_source"
            disposition = ProjectionReviewDisposition.RETAIN_REVIEW
            reason = "source id is absent from the canonical SQLite target"
        elif desired and all(
            item.action is ReconciliationAction.NOOP for item in desired
        ):
            source_state = "known_source_canonical_current"
            disposition = ProjectionReviewDisposition.CANDIDATE_DUPLICATE
            reason = "canonical projection is current; orphan can be considered after backup/policy approval"
        else:
            source_state = "known_source_canonical_not_current"
            disposition = ProjectionReviewDisposition.REPAIR_FIRST
            reason = "canonical projection is missing or stale; repair it before orphan cleanup"

        classifications.append(
            ProjectionReviewClassification(
                memory_id=source_id,
                collection_name=entry.collection_name,
                point_id=entry.point_id,
                surface=surface,
                source_state=source_state,
                disposition=disposition,
                reason=reason,
                project=str(payload.get("project") or "") or None,
                source_system=str(payload.get("source_system") or "") or None,
                observed_revision_id=entry.observed_revision_id,
                observed_lifecycle=str(payload.get("lifecycle") or "") or None,
            )
        )
    return tuple(classifications)


def projection_review_classification_digest(
    classifications: tuple[ProjectionReviewClassification, ...]
    | list[ProjectionReviewClassification],
) -> str:
    """Return a stable digest for the read-only orphan decision matrix."""

    payload = [item.to_dict() for item in classifications]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_basis_digest(memories: list[Any]) -> str:
    """Return an ephemeral generation fence derived only from SQLite state.

    The digest is deliberately not a second source of truth and is never
    persisted.  It lets apply detect a changed authoritative revision between
    dry-run and the first projection write.
    """

    basis = [
        {
            "memory_id": memory.id,
            "project": memory.project,
            "lifecycle": memory.lifecycle.value,
            "revision_id": memory.current_revision.revision_id,
            "collections": {
                collection_name: projection_payload_digest(memory, collection_name)
                for collection_name in QdrantProjector.collection_names(memory)
            },
        }
        for memory in memories
    ]
    return _digest_payload(
        sorted(basis, key=lambda item: (item["memory_id"], item["project"]))
    )

def _projection_generation(
    *,
    source_basis_digest: str,
    expected_dimensions: int | None,
    expected_embedding_model: str | None,
) -> str:
    """Name the read-only vector-space generation without storing it in Qdrant."""

    return _digest_payload(
        {
            "schema_version": "bhm.projection-generation.v1",
            "source_basis_digest": source_basis_digest,
            "expected_dimensions": expected_dimensions,
            "expected_embedding_model": expected_embedding_model or "unknown",
        }
    )


def _collection_compatibility(
    *,
    collection_name: str,
    points: list[dict[str, Any]],
    surface: ProjectionSurface,
    expected_dimensions: int | None,
    expected_embedding_model: str | None,
) -> ProjectionCompatibility:
    dimensions_reader = getattr(surface, "collection_vector_dimensions", None)
    observed_dimensions = (
        dimensions_reader(collection_name) if callable(dimensions_reader) else None
    )
    observed_models = tuple(
        sorted(
            {
                str(payload.get("projection_embedding_model"))
                for point in points
                for payload in [point.get("payload")]
                if isinstance(payload, Mapping)
                and str(payload.get("projection_embedding_model") or "").strip()
            }
        )
    )
    if not points and observed_dimensions is None:
        status = ProjectionCompatibilityStatus.MISSING
    elif expected_dimensions is not None and observed_dimensions != expected_dimensions:
        status = ProjectionCompatibilityStatus.DIMENSION_MISMATCH
    elif expected_embedding_model is not None and len(observed_models) > 1:
        status = ProjectionCompatibilityStatus.MIXED_MODEL
    elif (
        expected_embedding_model is not None
        and observed_models
        and observed_models != (expected_embedding_model,)
    ):
        status = ProjectionCompatibilityStatus.MODEL_MISMATCH
    elif expected_embedding_model is not None and points and not observed_models:
        # Pre-generation-marker payloads cannot safely be assumed to use the
        # currently configured model.  A reviewed recovery path must label or
        # replace them; this plan never guesses.
        status = ProjectionCompatibilityStatus.UNKNOWN
    else:
        status = ProjectionCompatibilityStatus.COMPATIBLE
    return ProjectionCompatibility(
        collection_name=collection_name,
        expected_dimensions=expected_dimensions,
        observed_dimensions=observed_dimensions,
        expected_embedding_model=expected_embedding_model,
        observed_embedding_models=observed_models,
        point_count=len(points),
        status=status,
    )


def _compatibility_receipt(
    *,
    collections: set[str],
    points_by_collection: Mapping[str, list[dict[str, Any]]],
    surface: ProjectionSurface,
    expected_dimensions: int | None,
    expected_embedding_model: str | None,
) -> tuple[ProjectionCompatibility, ...]:
    return tuple(
        _collection_compatibility(
            collection_name=collection_name,
            points=list(points_by_collection.get(collection_name, [])),
            surface=surface,
            expected_dimensions=expected_dimensions,
            expected_embedding_model=expected_embedding_model,
        )
        for collection_name in sorted(collections)
        if not collection_name.startswith(QDRANT_QUARANTINE_COLLECTION_PREFIX)
    )


def build_projection_reconciliation_plan(
    repository: MemoryRepository,
    surface: ProjectionSurface,
    *,
    project: str | None = None,
    as_of: str | None = None,
    expected_dimensions: int | None = None,
    expected_embedding_model: str | None = None,
    route_database: str | None = None,
    route_vector_dimensions: int | None = None,
    route_embedding_model: str | None = None,
    validation_budget: ValidationScanBudget | None = None,
) -> ProjectionReconciliationPlan:
    if validation_budget is not None:
        validation_budget.request("reconciliation.sqlite_memories")
    memories = repository.list_memories(
        project=project,
        include_archived=True,
        include_tombstoned=True,
        limit=10_000,
    )
    if route_database and (
        not isinstance(route_vector_dimensions, int)
        or route_vector_dimensions <= 0
        or not str(route_embedding_model or "").strip()
    ):
        raise ValueError(
            "bound projection reconciliation requires vector-space evidence"
        )

    entries: list[ReconciliationEntry] = []
    desired_keys: set[tuple[str, str]] = set()
    blocking: list[str] = []
    retained_rollback_collections: set[str] = set()
    if route_database:
        from .mem0_adapter import local_collection_name
        from .projection_generation_binding import active_projection_generation_bindings

        # A currently active binding retains its exact legacy local contour as
        # an operator-controlled rollback surface.  Its points are neither
        # desired on the active route nor orphaned projection data.  Treating
        # them as orphans makes a verified shadow switch permanently fail its
        # own read-only lifecycle audit.
        retained_rollback_collections = {
            local_collection_name(binding.project)
            for binding in active_projection_generation_bindings(route_database)
        }
    routed_memories: list[tuple[Any, tuple[str, ...]]] = []
    requested_ids: dict[str, list[str]] = {}
    routes_by_project: dict[tuple[str, str], Any] = {}
    for memory in memories:
        if validation_budget is not None:
            validation_budget.target("reconciliation.memory")
        collection_names = QdrantProjector.collection_names(memory)
        if route_database:
            # Import lazily: the binding module derives its source digest from
            # this module, while reconciliation merely consumes its read-only
            # route decision.
            from .projection_generation_binding import resolve_projection_route

            # A projection route is scoped to a project and its deterministic
            # legacy collection, never to an individual memory.  Resolving the
            # binding re-reads and verifies SQLite authority; repeating that
            # same proof for every row makes a read-only lifecycle validator
            # quadratic enough to exhaust its deadline before it can inspect
            # the actual Qdrant assertion.  Cache only within this invocation
            # so no route evidence survives the plan that consumed it.
            route_key = (memory.project, collection_names[0])
            route = routes_by_project.get(route_key)
            if route is None:
                route = resolve_projection_route(
                    project=memory.project,
                    legacy_collection=collection_names[0],
                    database=route_database,
                    embedding_model=str(route_embedding_model),
                    vector_dimensions=int(route_vector_dimensions),
                    distance="Cosine",
                )
                routes_by_project[route_key] = route
            if route.collection_name is None:
                # A project with retired/bound routing must never be silently
                # reconciled against its legacy collection.  Preserve any
                # global contour but make the unavailable local route a
                # fail-closed review blocker.
                blocking.append(f"route:{memory.project}:{route.reason}")
                collection_names = collection_names[1:]
            else:
                collection_names = (route.collection_name, *collection_names[1:])
        routed_memories.append((memory, tuple(collection_names)))
        for collection_name in collection_names:
            point_id = deterministic_point_id(collection_name, memory.id)
            requested_ids.setdefault(collection_name, []).append(point_id)

    observed_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    batch_reader = getattr(surface, "get_points", None)
    for collection_name, point_ids in sorted(requested_ids.items()):
        if callable(batch_reader):
            batch = batch_reader(collection_name, point_ids)
            if not isinstance(batch, Mapping):
                raise RuntimeError("projection batch reader returned an invalid result")
            for point_id, point in batch.items():
                if isinstance(point, Mapping):
                    observed_by_key[(collection_name, str(point_id))] = dict(point)
        else:
            for point_id in point_ids:
                observed = surface.get_point(collection_name, point_id)
                if observed is not None:
                    observed_by_key[(collection_name, point_id)] = observed

    for memory, collection_names in routed_memories:
        for collection_name in collection_names:
            point_id = deterministic_point_id(collection_name, memory.id)
            desired_keys.add((collection_name, point_id))
            observed = observed_by_key.get((collection_name, point_id))
            observed_payload = (observed or {}).get("payload") if observed else None
            observed_revision = (
                str(observed_payload.get("revision_id"))
                if isinstance(observed_payload, Mapping)
                and observed_payload.get("revision_id")
                else None
            )
            desired_digest = projection_payload_digest(memory, collection_name)
            observed_marker = (
                str(observed_payload.get("projection_payload_digest"))
                if isinstance(observed_payload, Mapping)
                and observed_payload.get("projection_payload_digest")
                else None
            )
            observed_digest = (
                projection_payload_digest_from_payload(observed_payload)
                if isinstance(observed_payload, Mapping)
                else None
            )
            if memory.lifecycle is Lifecycle.TOMBSTONED:
                action = (
                    ReconciliationAction.DELETE
                    if observed
                    else ReconciliationAction.NOOP
                )
                reason = "tombstone cleanup" if observed else "tombstone already absent"
            elif observed is None:
                action = ReconciliationAction.UPSERT
                reason = "projection missing"
            elif (
                observed_revision != memory.current_revision.revision_id
                or str(observed_payload.get("lifecycle")) != memory.lifecycle.value
                or observed_marker != desired_digest
                or observed_digest != desired_digest
            ):
                action = ReconciliationAction.UPSERT
                reason = "projection payload stale"
            else:
                action = ReconciliationAction.NOOP
                reason = "projection matches"
            entries.append(
                ReconciliationEntry(
                    memory_id=memory.id,
                    collection_name=collection_name,
                    point_id=point_id,
                    action=action,
                    reason=reason,
                    desired_revision_id=memory.current_revision.revision_id,
                    observed_revision_id=observed_revision,
                    desired_payload_digest=desired_digest,
                    observed_payload_digest=observed_digest,
                    observed_payload=observed_payload,
                )
            )

    canonical_collections = {entry.collection_name for entry in entries}
    collections = set(surface.list_collections()) | canonical_collections
    compatibility_collections = set(canonical_collections)
    points_by_collection: dict[str, list[dict[str, Any]]] = {}
    for collection_name in sorted(collections):
        if collection_name.startswith(QDRANT_QUARANTINE_COLLECTION_PREFIX):
            continue
        if validation_budget is not None:
            validation_budget.target("reconciliation.collection")
        points = surface.list_points(collection_name)
        points_by_collection[collection_name] = points
        if collection_name in retained_rollback_collections:
            continue
        if project is None or any(
            isinstance(point.get("payload"), Mapping)
            and str(point["payload"].get("project") or "") == project
            for point in points
        ):
            compatibility_collections.add(collection_name)
        for point in points:
            point_id = str(point.get("id") or "")
            if not point_id or (collection_name, point_id) in desired_keys:
                continue
            payload = (
                point.get("payload")
                if isinstance(point.get("payload"), Mapping)
                else {}
            )
            point_project = str(payload.get("project") or "")
            if project is not None and point_project != project:
                continue
            memory_id = str(payload.get("source_id") or "") or None
            entries.append(
                ReconciliationEntry(
                    memory_id=memory_id,
                    collection_name=collection_name,
                    point_id=point_id,
                    action=ReconciliationAction.REVIEW,
                    reason="orphan projection requires explicit delete approval",
                    observed_revision_id=str(payload.get("revision_id") or "") or None,
                    observed_payload=dict(payload),
                )
            )
            blocking.append(f"orphan:{collection_name}:{point_id}")

    entries.sort(
        key=lambda entry: (entry.collection_name, entry.point_id, entry.action.value)
    )
    compatibility = _compatibility_receipt(
        collections=compatibility_collections,
        points_by_collection=points_by_collection,
        surface=surface,
        expected_dimensions=expected_dimensions,
        expected_embedding_model=expected_embedding_model,
    )
    blocking.extend(
        f"compatibility:{item.collection_name}:{item.status.value}"
        for item in compatibility
        if item.blocking
    )
    source_basis_digest = _source_basis_digest(memories)
    return ProjectionReconciliationPlan(
        as_of=as_of or _now_iso(),
        project=project,
        entries=tuple(entries),
        compatibility=compatibility,
        source_basis_digest=source_basis_digest,
        projection_generation=_projection_generation(
            source_basis_digest=source_basis_digest,
            expected_dimensions=expected_dimensions,
            expected_embedding_model=expected_embedding_model,
        ),
        blocking_issues=tuple(sorted(blocking)),
    )


def apply_projection_reconciliation(
    plan: ProjectionReconciliationPlan,
    repository: MemoryRepository,
    projector: QdrantProjector,
    surface: ProjectionSurface,
    *,
    allow_orphan_delete: bool = False,
) -> ProjectionApplyResult:
    upserted = 0
    deleted = 0
    reviewed = 0
    failures: list[str] = []
    if any(item.blocking for item in plan.compatibility):
        return ProjectionApplyResult(
            plan_digest=plan.digest,
            upserted=0,
            deleted=0,
            reviewed=0,
            failed=(
                "plan contains blocking projection compatibility mismatch; no mutation attempted",
            ),
        )
    try:
        if plan.source_basis_digest:
            current_memories = repository.list_memories(
                project=plan.project,
                include_archived=True,
                include_tombstoned=True,
                limit=10_000,
            )
            if _source_basis_digest(current_memories) != plan.source_basis_digest:
                return ProjectionApplyResult(
                    plan_digest=plan.digest,
                    upserted=0,
                    deleted=0,
                    reviewed=0,
                    failed=(
                        "authoritative SQLite generation changed after reconciliation plan; no mutation attempted",
                    ),
                )
        if not plan.compatibility:
            current_compatibility: tuple[ProjectionCompatibility, ...] = ()
        else:
            expected_dimensions = next(
                (
                    item.expected_dimensions
                    for item in plan.compatibility
                    if item.expected_dimensions is not None
                ),
                None,
            )
            expected_embedding_model = next(
                (
                    item.expected_embedding_model
                    for item in plan.compatibility
                    if item.expected_embedding_model
                ),
                None,
            )
            collections = {item.collection_name for item in plan.compatibility}
            points_by_collection = {
                collection_name: surface.list_points(collection_name)
                for collection_name in collections
            }
            current_compatibility = _compatibility_receipt(
                collections=collections,
                points_by_collection=points_by_collection,
                surface=surface,
                expected_dimensions=expected_dimensions,
                expected_embedding_model=expected_embedding_model,
            )
    except Exception as exc:  # broad: fail closed when projection preflight is unavailable
        return ProjectionApplyResult(
            plan_digest=plan.digest,
            upserted=0,
            deleted=0,
            reviewed=0,
            failed=(
                f"projection preflight unavailable; no mutation attempted: {type(exc).__name__}",
            ),
        )
    if plan.compatibility and [item.to_dict() for item in current_compatibility] != [
        item.to_dict() for item in plan.compatibility
    ]:
        return ProjectionApplyResult(
            plan_digest=plan.digest,
            upserted=0,
            deleted=0,
            reviewed=0,
            failed=(
                "projection compatibility or generation changed after reconciliation plan; no mutation attempted",
            ),
        )
    for entry in plan.entries:
        try:
            if entry.action is ReconciliationAction.NOOP:
                continue
            if entry.action is ReconciliationAction.REVIEW:
                if not allow_orphan_delete:
                    reviewed += 1
                    continue
                _revalidate_review_delete(plan, entry, surface)
                surface.delete_point(entry.collection_name, entry.point_id)
                deleted += 1
                continue
            if entry.memory_id is None:
                failures.append(
                    f"{entry.collection_name}:{entry.point_id}:missing-memory-id"
                )
                continue
            memory = repository.get_memory(entry.memory_id, project=plan.project)
            if memory is None:
                failures.append(
                    f"{entry.collection_name}:{entry.point_id}:memory-not-found"
                )
                continue
            if entry.action is ReconciliationAction.DELETE:
                # Re-read authority immediately before deleting the projection;
                # the first lookup only proves the plan had a source row.
                memory = repository.get_memory(entry.memory_id, project=plan.project)
                if memory is None:
                    failures.append(
                        f"{entry.collection_name}:{entry.point_id}:memory-not-found"
                    )
                    continue
                _revalidate_tombstone_delete(plan, entry, memory, surface)
                surface.delete_point(entry.collection_name, entry.point_id)
                deleted += 1
            elif entry.action is ReconciliationAction.UPSERT:
                projector.project_memory(
                    memory,
                    event_id=f"reconcile:{plan.digest[:24]}:{memory.id}",
                )
                upserted += 1
        except Exception as exc:  # broad: record per-entry projection failures and continue reconciliation
            failures.append(
                f"{entry.collection_name}:{entry.point_id}:{type(exc).__name__}:{exc}"
            )
    return ProjectionApplyResult(
        plan_digest=plan.digest,
        upserted=upserted,
        deleted=deleted,
        reviewed=reviewed,
        failed=tuple(failures),
    )
