"""Qdrant surface adapter that treats a confirmed-missing collection as empty.

Read-only reconciliation reports (orphan classification, lifecycle) must not
abort because one project has no local collection; any other Qdrant error
still propagates.
"""

from __future__ import annotations

from typing import Any

from qdrant_client.http.exceptions import UnexpectedResponse

from .projection_reconciliation import QdrantSurfaceAdapter


class MissingCollectionTolerantSurface(QdrantSurfaceAdapter):
    """Treat only a confirmed missing collection as an empty surface."""

    def __init__(self, client: Any, **kwargs: Any) -> None:
        super().__init__(client, **kwargs)
        self.missing_collections: dict[str, dict[str, Any]] = {}

    def _missing(self, collection_name: str, exc: UnexpectedResponse) -> bool:
        try:
            detail = exc.structured()["status"]["error"]
        except (ValueError, KeyError, TypeError):
            return False
        expected = f"Not found: Collection `{collection_name}` doesn't exist"
        if exc.status_code != 404 or detail not in (expected, expected + "!"):
            return False
        self.missing_collections[collection_name] = {
            "collection": collection_name, "reason": detail, "pointCount": 0
        }
        return True

    def get_points(self, collection_name: str, point_ids: Any) -> dict[str, dict[str, Any]]:
        try:
            return super().get_points(collection_name, point_ids)
        except UnexpectedResponse as exc:
            if not self._missing(collection_name, exc):
                raise
            return {}

    def list_points(self, collection_name: str) -> list[dict[str, Any]]:
        try:
            return super().list_points(collection_name)
        except UnexpectedResponse as exc:
            if not self._missing(collection_name, exc):
                raise
            return []

    def collection_vector_dimensions(self, collection_name: str) -> int | None:
        try:
            return super().collection_vector_dimensions(collection_name)
        except UnexpectedResponse as exc:
            if not self._missing(collection_name, exc):
                raise
            return None
