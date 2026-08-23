"""Immutable, content-free ontology admission worklists.

Rejected ontology relation writes must remain visible to an operator without
turning the rejected link into graph authority. This module only creates and
serializes SQLite artifact records; it never writes a link, Qdrant point, or
Mem0 record.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .domain import Artifact
from .ontology_registry import OntologyRelationAdmissionReceipt


ARTIFACT_TYPE = "ontology_quarantine"
SCHEMA_VERSION = "bhm.ontology-quarantine.v1"


def _identity(receipt: OntologyRelationAdmissionReceipt) -> dict[str, str]:
    return {
        "project": receipt.project,
        "schema_digest": receipt.schema_digest,
        "reason_code": receipt.reason_code,
        "relation": receipt.relation,
        "source_id_digest": receipt.source_id_digest,
        "target_id_digest": receipt.target_id_digest,
    }


def _digest(value: Mapping[str, str]) -> str:
    encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_quarantine_artifact(receipt: OntologyRelationAdmissionReceipt) -> Artifact:
    """Build a replay-safe immutable worklist artifact for one rejection."""

    if receipt.decision != "quarantine":
        raise ValueError("only a quarantined ontology receipt can create a worklist artifact")
    identity = _identity(receipt)
    event_digest = _digest(identity)
    return Artifact(
        id=f"ontology_quarantine_{event_digest[:32]}",
        artifact_type=ARTIFACT_TYPE,
        project=receipt.project,
        payload={
            "schema_version": SCHEMA_VERSION,
            "event_digest": event_digest,
            "schema_digest": receipt.schema_digest,
            "reason_code": receipt.reason_code,
            "relation": receipt.relation,
            "source_type": receipt.source_type,
            "target_type": receipt.target_type,
            "source_id_digest": receipt.source_id_digest,
            "target_id_digest": receipt.target_id_digest,
            "content_free": True,
            "requires_review": True,
            "review_state": "open",
            "authority": "sqlite-authoritative",
            "link_storage_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
        },
    )


def serialize_quarantine_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the bounded, content-free operator view of a worklist item."""

    return {
        "id": str(record.get("id") or ""),
        "project": str(record.get("project") or ""),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "schema_version": str(record.get("schema_version") or ""),
        "event_digest": str(record.get("event_digest") or ""),
        "schema_digest": str(record.get("schema_digest") or ""),
        "reason_code": str(record.get("reason_code") or ""),
        "relation": str(record.get("relation") or ""),
        "source_type": record.get("source_type"),
        "target_type": record.get("target_type"),
        "source_id_digest": str(record.get("source_id_digest") or ""),
        "target_id_digest": str(record.get("target_id_digest") or ""),
        "content_free": bool(record.get("content_free")),
        "requires_review": bool(record.get("requires_review")),
        "review_state": str(record.get("review_state") or "open"),
        "execution": {
            "link_storage_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
        },
    }
