from __future__ import annotations

from dataclasses import dataclass

import pytest

from blackholememory.projection_quarantine import ProjectionQuarantineError
from blackholememory.projection_quarantine import build_quarantine_payload
from blackholememory.projection_quarantine import candidate_classifications
from blackholememory.projection_quarantine import collect_quarantine_points
from blackholememory.projection_quarantine import quarantine_point_id
from blackholememory.projection_quarantine import quarantine_classifications
from blackholememory.projection_reconciliation import ProjectionReviewClassification
from blackholememory.projection_reconciliation import ProjectionReviewDisposition


@dataclass
class _Point:
    id: str
    payload: dict
    vector: list[float]


class _Client:
    def __init__(self, points: list[_Point]) -> None:
        self.points = points

    def scroll(self, *, collection_name, limit, offset, with_payload, with_vectors):
        return [point for point in self.points if collection_name == "legacy"], None


def _classification(
    point_id: str,
    disposition: ProjectionReviewDisposition,
    *,
    memory_id: str | None = "mem_bhm_known",
) -> ProjectionReviewClassification:
    return ProjectionReviewClassification(
        memory_id=memory_id,
        collection_name="legacy",
        point_id=point_id,
        surface="legacy_mem0",
        source_state="known_source_canonical_current",
        disposition=disposition,
        reason="test",
        project="blackholememory",
        source_system="bhm",
    )


def test_quarantine_point_id_and_payload_are_stable():
    point_id = quarantine_point_id("batch-1", "legacy", "point-1")
    assert point_id == quarantine_point_id("batch-1", "legacy", "point-1")
    point = collect_quarantine_points(
        _Client([_Point("point-1", {"source_id": "mem_bhm_known"}, [0.1, 0.9])]),
        [_classification("point-1", ProjectionReviewDisposition.CANDIDATE_DUPLICATE)],
        batch_id="batch-1",
    )[0]

    payload = build_quarantine_payload(
        point,
        batch_id="batch-1",
        quarantine_collection="bhm_quarantine_projection_batch_1",
    )
    assert payload["source_id"] == "mem_bhm_known"
    assert payload["_bhm_quarantine"]["original_point_id"] == "point-1"
    assert payload["_bhm_quarantine"]["batch_id"] == "batch-1"


def test_candidate_classifications_reject_repair_first_entries():
    with pytest.raises(ProjectionQuarantineError, match="require projection repair"):
        candidate_classifications(
            [
                _classification("known", ProjectionReviewDisposition.CANDIDATE_DUPLICATE),
                _classification("stale", ProjectionReviewDisposition.REPAIR_FIRST),
            ]
        )


def test_quarantine_classifications_selects_retain_review_entries():
    selected = quarantine_classifications(
        [
            _classification("duplicate", ProjectionReviewDisposition.CANDIDATE_DUPLICATE),
            _classification("review", ProjectionReviewDisposition.RETAIN_REVIEW),
        ],
        disposition=ProjectionReviewDisposition.RETAIN_REVIEW,
    )

    assert [item.point_id for item in selected] == ["review"]


def test_collect_quarantine_points_fails_closed_when_candidate_disappears():
    with pytest.raises(ProjectionQuarantineError, match="missing"):
        collect_quarantine_points(
            _Client([]),
            [_classification("missing", ProjectionReviewDisposition.CANDIDATE_DUPLICATE)],
            batch_id="batch-1",
        )
