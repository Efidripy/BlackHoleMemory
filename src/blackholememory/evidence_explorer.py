"""Read-only evidence explorer and non-persisting correction proposal receipts.

The explorer combines already-derived SQLite claim history with supplied,
content-free retrieval and projection diagnostics. It does not load a provider,
query projections or write a governed proposal. The proposal envelope is for a
future explicitly authorized handoff to the existing governed queue.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


SCHEMA_VERSION = "bhm.evidence-explorer.v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,219}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE = re.compile(r"(?:bearer|password|secret|token|authorization|akia[0-9a-z]{16})", re.I)
_PROJECTION_STATES = {"ready", "degraded", "blocked", "unknown"}
_CORRECTION_KINDS = {"supersede_claim", "contradict_claim", "request_evidence_review"}


class EvidenceExplorerError(ValueError):
    """Raised when inspect or correction evidence crosses a safety boundary."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identifier(value: Any, field_name: str) -> str:
    result = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(result) or _SENSITIVE.search(result):
        raise EvidenceExplorerError(f"{field_name} is invalid")
    return result


def _sha256(value: Any, field_name: str) -> str:
    result = str(value or "").strip().lower()
    if not _SHA256.fullmatch(result):
        raise EvidenceExplorerError(f"{field_name} must be a SHA-256 digest")
    return result


def _claim_row(claims: Mapping[str, Any], claim_id: str) -> Mapping[str, Any]:
    if claims.get("schema_version") != "bhm.claims-evidence-read-model.v1":
        raise EvidenceExplorerError("claim read model schema is invalid")
    rows = claims.get("claims")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise EvidenceExplorerError("claim read model is invalid")
    matching = [row for row in rows if isinstance(row, Mapping) and row.get("claim_id") == claim_id]
    if len(matching) != 1:
        raise EvidenceExplorerError("claim is missing or ambiguous")
    return matching[0]


def _safe_history(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    history = row.get("history")
    if not isinstance(history, list) or len(history) > 128:
        raise EvidenceExplorerError("claim history is invalid")
    result = []
    for item in history:
        if not isinstance(item, Mapping):
            raise EvidenceExplorerError("claim history item is invalid")
        result.append(
            {
                "assertion_id": _identifier(item.get("assertion_id"), "assertion_id"),
                "assertion_digest": _sha256(item.get("assertion_digest"), "assertion_digest"),
                "memory_id": _identifier(item.get("memory_id"), "memory_id"),
                "revision_id": _identifier(item.get("revision_id"), "revision_id"),
                "disposition": str(item.get("disposition") or "unknown"),
                "evidence_source_count": int(item.get("evidence_source_count") or 0),
            }
        )
    return result


def build_evidence_explorer_view(
    claim_read_model: Mapping[str, Any],
    *,
    project: str,
    claim_id: str,
    retrieval_receipt: Mapping[str, Any],
    projection_state: str,
) -> dict[str, Any]:
    """Build a content-free read-only inspection model for one SQLite claim."""

    normalized_project = _identifier(project, "project")
    normalized_claim = _identifier(claim_id, "claim_id")
    if claim_read_model.get("project") != normalized_project:
        raise EvidenceExplorerError("claim read model project mismatch")
    if projection_state not in _PROJECTION_STATES:
        raise EvidenceExplorerError("projection state is invalid")
    row = _claim_row(claim_read_model, normalized_claim)
    history = _safe_history(row)
    if not isinstance(retrieval_receipt, Mapping):
        raise EvidenceExplorerError("retrieval receipt is invalid")
    retrieval_digest = _sha256(retrieval_receipt.get("receipt_digest"), "retrieval receipt digest")
    retrieval_project = retrieval_receipt.get("project")
    if retrieval_project is not None and retrieval_project != normalized_project:
        raise EvidenceExplorerError("retrieval receipt project mismatch")
    core = {
        "schema_version": SCHEMA_VERSION,
        "project": normalized_project,
        "claim_id": normalized_claim,
        "claim_state": str(row.get("state") or "unknown"),
        "history": history,
        "retrieval_receipt_digest": retrieval_digest,
        "projection_state": projection_state,
    }
    return {
        **core,
        "view_digest": _digest(core),
        "execution": {
            "read_only": True,
            "sqlite_mutation": False,
            "proposal_persisted": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "graph_mutation": False,
        },
    }


def verify_evidence_explorer_view(view: Mapping[str, Any]) -> bool:
    core = {key: view.get(key) for key in ("schema_version", "project", "claim_id", "claim_state", "history", "retrieval_receipt_digest", "projection_state")}
    try:
        _identifier(core["project"], "project")
        _identifier(core["claim_id"], "claim_id")
        _sha256(core["retrieval_receipt_digest"], "retrieval receipt digest")
        _safe_history({"history": core["history"]})
    except EvidenceExplorerError:
        return False
    return (
        core["schema_version"] == SCHEMA_VERSION
        and core["projection_state"] in _PROJECTION_STATES
        and view.get("view_digest") == _digest(core)
        and view.get("execution") == {"read_only": True, "sqlite_mutation": False, "proposal_persisted": False, "mem0_mutation": False, "qdrant_mutation": False, "graph_mutation": False}
    )


def build_governed_correction_proposal(
    view: Mapping[str, Any],
    *,
    correction_kind: str,
    target_assertion_id: str,
    rationale_digest: str,
) -> dict[str, Any]:
    """Return a dry-run correction envelope, never an authority write request."""

    if not verify_evidence_explorer_view(view):
        raise EvidenceExplorerError("explorer view digest is invalid")
    if correction_kind not in _CORRECTION_KINDS:
        raise EvidenceExplorerError("correction kind is invalid")
    target = _identifier(target_assertion_id, "target_assertion_id")
    if target not in {item["assertion_id"] for item in view["history"]}:
        raise EvidenceExplorerError("correction target is outside explorer history")
    core = {
        "schema_version": SCHEMA_VERSION,
        "proposal_kind": correction_kind,
        "project": view["project"],
        "claim_id": view["claim_id"],
        "target_assertion_id": target,
        "view_digest": view["view_digest"],
        "rationale_digest": _sha256(rationale_digest, "rationale_digest"),
    }
    return {
        **core,
        "proposal_digest": _digest(core),
        "handoff": {"destination": "existing-governed-proposal-queue", "persisted": False, "explicit_confirmation_required": True},
        "execution": {"dry_run": True, "sqlite_mutation": False, "direct_authority_write": False, "outbox_mutation": False},
        "rollback": "discard-proposal-and-retain-current-claim-history",
    }


__all__ = [
    "EvidenceExplorerError",
    "SCHEMA_VERSION",
    "build_evidence_explorer_view",
    "build_governed_correction_proposal",
    "verify_evidence_explorer_view",
]
