"""Reversible Qdrant quarantine primitives for reviewed projection orphans."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from qdrant_client.http import models as qdrant_models

from .projection_reconciliation import ProjectionReviewClassification
from .projection_reconciliation import ProjectionReviewDisposition


QUARANTINE_SCHEMA_VERSION = "1.0"


class ProjectionQuarantineError(RuntimeError):
    """Raised when a reversible quarantine cannot be completed safely."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def quarantine_point_id(batch_id: str, collection_name: str, point_id: str) -> str:
    """Build a stable UUID for a point inside one quarantine batch."""

    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"blackholememory:projection-quarantine:{batch_id}:{collection_name}:{point_id}",
        )
    )


@dataclass(frozen=True)
class QuarantinePoint:
    """Original Qdrant point plus reversible quarantine identity."""

    original_collection: str
    original_id: Any
    original_point_id: str
    quarantine_point_id: str
    payload: dict[str, Any]
    vector: Any

    @property
    def payload_sha256(self) -> str:
        return json_sha256(self.payload)

    @property
    def vector_sha256(self) -> str:
        return json_sha256(self.vector)

    def backup_dict(self) -> dict[str, Any]:
        return {
            "originalCollection": self.original_collection,
            "originalId": str(self.original_id),
            "originalPointId": self.original_point_id,
            "quarantinePointId": self.quarantine_point_id,
            "payload": copy.deepcopy(self.payload),
            "vector": copy.deepcopy(self.vector),
            "payloadSha256": self.payload_sha256,
            "vectorSha256": self.vector_sha256,
        }


def candidate_classifications(
    classifications: Iterable[ProjectionReviewClassification],
) -> tuple[ProjectionReviewClassification, ...]:
    """Return only entries eligible for reviewed duplicate quarantine."""

    return quarantine_classifications(
        classifications,
        disposition=ProjectionReviewDisposition.CANDIDATE_DUPLICATE,
    )


def quarantine_classifications(
    classifications: Iterable[ProjectionReviewClassification],
    *,
    disposition: ProjectionReviewDisposition,
) -> tuple[ProjectionReviewClassification, ...]:
    """Select one reviewed disposition for reversible quarantine."""

    selected = tuple(
        item
        for item in classifications
        if item.disposition is disposition
    )
    repair_first = [
        item
        for item in classifications
        if item.disposition is ProjectionReviewDisposition.REPAIR_FIRST
    ]
    if repair_first:
        raise ProjectionQuarantineError(
            f"{len(repair_first)} review entries require projection repair before quarantine"
        )
    return selected


def collect_quarantine_points(
    client: Any,
    classifications: Iterable[ProjectionReviewClassification],
    *,
    batch_id: str,
    disposition: ProjectionReviewDisposition = ProjectionReviewDisposition.CANDIDATE_DUPLICATE,
) -> tuple[QuarantinePoint, ...]:
    """Read and verify every candidate point before any Qdrant write."""

    selected = quarantine_classifications(classifications, disposition=disposition)
    wanted: dict[str, set[str]] = defaultdict(set)
    for item in selected:
        wanted[item.collection_name].add(item.point_id)

    collected: list[QuarantinePoint] = []
    found: dict[str, set[str]] = defaultdict(set)
    for collection_name in sorted(wanted):
        offset: Any = None
        while True:
            points, offset = client.scroll(
                collection_name=collection_name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for point in points:
                point_id = str(point.id)
                if point_id not in wanted[collection_name]:
                    continue
                vector = getattr(point, "vector", None)
                if vector is None:
                    raise ProjectionQuarantineError(
                        f"candidate point has no vector: {collection_name}:{point_id}"
                    )
                payload = dict(getattr(point, "payload", None) or {})
                collected.append(
                    QuarantinePoint(
                        original_collection=collection_name,
                        original_id=point.id,
                        original_point_id=point_id,
                        quarantine_point_id=quarantine_point_id(batch_id, collection_name, point_id),
                        payload=payload,
                        vector=vector,
                    )
                )
                found[collection_name].add(point_id)
            if offset is None or not points:
                break

    missing = {
        f"{collection}:{point_id}"
        for collection, point_ids in wanted.items()
        for point_id in point_ids - found[collection]
    }
    if missing:
        sample = ", ".join(sorted(missing)[:8])
        raise ProjectionQuarantineError(
            f"candidate set changed before backup; missing {len(missing)} points: {sample}"
        )
    if len(collected) != len(selected):
        raise ProjectionQuarantineError(
            f"candidate cardinality mismatch: expected {len(selected)}, got {len(collected)}"
        )
    collected.sort(key=lambda item: (item.original_collection, item.original_point_id))
    return tuple(collected)


def build_quarantine_payload(
    point: QuarantinePoint,
    *,
    batch_id: str,
    quarantine_collection: str,
) -> dict[str, Any]:
    payload = copy.deepcopy(point.payload)
    payload["_bhm_quarantine"] = {
        "schema_version": QUARANTINE_SCHEMA_VERSION,
        "batch_id": batch_id,
        "quarantine_collection": quarantine_collection,
        "original_collection": point.original_collection,
        "original_point_id": point.original_point_id,
        "payload_sha256": point.payload_sha256,
        "vector_sha256": point.vector_sha256,
    }
    return payload


def ensure_quarantine_collection(
    client: Any,
    collection_name: str,
    *,
    dimensions: int,
) -> None:
    if client.collection_exists(collection_name):
        raise ProjectionQuarantineError(
            f"quarantine collection already exists: {collection_name}"
        )
    client.create_collection(
        collection_name=collection_name,
        vectors_config=qdrant_models.VectorParams(
            size=dimensions,
            distance=qdrant_models.Distance.COSINE,
        ),
    )


def upsert_quarantine_points(
    client: Any,
    collection_name: str,
    points: Iterable[QuarantinePoint],
    *,
    batch_id: str,
    batch_size: int = 64,
) -> int:
    point_list = list(points)
    for start in range(0, len(point_list), batch_size):
        batch = point_list[start : start + batch_size]
        client.upsert(
            collection_name=collection_name,
            points=[
                qdrant_models.PointStruct(
                    id=point.quarantine_point_id,
                    vector=point.vector,
                    payload=build_quarantine_payload(
                        point,
                        batch_id=batch_id,
                        quarantine_collection=collection_name,
                    ),
                )
                for point in batch
            ],
            wait=True,
        )
    return len(point_list)


def verify_quarantine_points(
    client: Any,
    collection_name: str,
    points: Iterable[QuarantinePoint],
) -> None:
    expected = {point.quarantine_point_id for point in points}
    observed: set[str] = set()
    offset: Any = None
    while True:
        records, offset = client.scroll(
            collection_name=collection_name,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        observed.update(str(record.id) for record in records)
        if offset is None or not records:
            break
    if observed != expected:
        raise ProjectionQuarantineError(
            f"quarantine verification mismatch: expected {len(expected)}, observed {len(observed)}"
        )


def delete_original_points(
    client: Any,
    points: Iterable[QuarantinePoint],
    *,
    batch_size: int = 128,
) -> int:
    grouped: dict[tuple[str, str], list[QuarantinePoint]] = defaultdict(list)
    for point in points:
        project = str(point.payload.get("project") or "").strip()
        if not project:
            raise ProjectionQuarantineError(
                f"cannot delete unscoped projection point: "
                f"{point.original_collection}:{point.original_point_id}"
            )
        grouped[(point.original_collection, project)].append(point)

    deleted = 0
    for (collection_name, project) in sorted(grouped):
        point_list = grouped[(collection_name, project)]
        for start in range(0, len(point_list), batch_size):
            batch_points = point_list[start : start + batch_size]
            point_ids = [point.original_id for point in batch_points]
            observed = client.retrieve(
                collection_name=collection_name,
                ids=point_ids,
                with_payload=True,
                with_vectors=True,
            )
            observed_by_id = {str(record.id): record for record in observed or []}
            expected_ids = {point.original_point_id for point in batch_points}
            if set(observed_by_id) != expected_ids:
                raise ProjectionQuarantineError(
                    f"candidate set changed before delete: {collection_name}:{project}"
                )
            for point in batch_points:
                record = observed_by_id[point.original_point_id]
                payload = dict(getattr(record, "payload", None) or {})
                vector = getattr(record, "vector", None)
                if str(payload.get("project") or "").strip() != project:
                    raise ProjectionQuarantineError(
                        f"candidate crossed project boundary: {collection_name}:{point.original_point_id}"
                    )
                if json_sha256(payload) != point.payload_sha256:
                    raise ProjectionQuarantineError(
                        f"candidate payload changed before delete: {collection_name}:{point.original_point_id}"
                    )
                if json_sha256(vector) != point.vector_sha256:
                    raise ProjectionQuarantineError(
                        f"candidate vector changed before delete: {collection_name}:{point.original_point_id}"
                    )
            selector = qdrant_models.FilterSelector(
                filter=qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="project",
                            match=qdrant_models.MatchValue(value=project),
                        ),
                        qdrant_models.HasIdCondition(has_id=point_ids),
                    ]
                )
            )
            client.delete(
                collection_name=collection_name,
                points_selector=selector,
                wait=True,
            )
            deleted += len(batch_points)
    return deleted


def verify_original_points_absent(
    client: Any,
    points: Iterable[QuarantinePoint],
    *,
    batch_size: int = 128,
) -> None:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for point in points:
        grouped[point.original_collection].append(point.original_id)
    remaining: list[str] = []
    for collection_name in sorted(grouped):
        point_ids = grouped[collection_name]
        for start in range(0, len(point_ids), batch_size):
            batch = point_ids[start : start + batch_size]
            records = client.retrieve(
                collection_name=collection_name,
                ids=batch,
                with_payload=False,
                with_vectors=False,
            )
            remaining.extend(f"{collection_name}:{record.id}" for record in records)
    if remaining:
        sample = ", ".join(remaining[:8])
        raise ProjectionQuarantineError(
            f"original points remain after quarantine: {len(remaining)} ({sample})"
        )
