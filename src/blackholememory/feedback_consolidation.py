"""Build bounded, operator-reviewable consolidation proposals from utility events.

The utility score is deliberately not a lifecycle control.  This module turns
only sufficiently supported negative feedback into content-free review items;
it cannot merge, archive, tombstone, rewrite, queue, or otherwise mutate BHM.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "bhm.feedback-consolidation-preview.v1"
UTILITY_REPORT_SCHEMA_VERSION = "bhm.utility-feedback.v1"
MAX_REPORT_ROWS = 10_000
MAX_PROPOSALS = 256
MIN_SUPPORTED_SAMPLES = 3
LOW_UTILITY_SCORE = -0.25


class FeedbackConsolidationError(ValueError):
    """Raised if an unbound or unsafe utility report is supplied."""


def build_feedback_consolidation_preview(
    utility: Mapping[str, Any],
    *,
    project: str,
    max_proposals: int = MAX_PROPOSALS,
) -> dict[str, Any]:
    """Return deterministic, content-free review items without changing authority.

    The caller must bind the worklist to one project and an exact immutable
    utility report.  Negative feedback provides a reason to review evidence,
    never a reason to automatically lower lifecycle, merge memory, or change a
    retrieval ranker.
    """

    project_name = _required_text(project, "project")
    bounded_max = int(max_proposals)
    if not 1 <= bounded_max <= MAX_PROPOSALS:
        raise FeedbackConsolidationError(f"max_proposals must be within 1..{MAX_PROPOSALS}")
    if str(utility.get("schema_version") or "") != UTILITY_REPORT_SCHEMA_VERSION:
        raise FeedbackConsolidationError("utility report schema_version is unsupported")
    report_digest = _required_digest(utility.get("report_digest"), "utility.report_digest")
    rows = utility.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise FeedbackConsolidationError("utility report rows must be an array")
    if len(rows) > MAX_REPORT_ROWS:
        raise FeedbackConsolidationError(f"utility report rows exceed {MAX_REPORT_ROWS}")

    proposals: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise FeedbackConsolidationError("utility report row must be an object")
        proposals.extend(_row_proposals(raw, project=project_name, report_digest=report_digest))
    ordered = sorted(
        proposals,
        key=lambda item: (
            -int(item["evidence_count"]),
            item["review_kind"],
            item["memory_id"],
            item["proposal_id"],
        ),
    )[:bounded_max]
    core = {
        "schema_version": SCHEMA_VERSION,
        "project": project_name,
        "source_utility_report_digest": report_digest,
        "proposal_count": len(ordered),
        "omitted_count": max(len(proposals) - len(ordered), 0),
        "proposals": ordered,
        "thresholds": {
            "min_supported_samples": MIN_SUPPORTED_SAMPLES,
            "low_utility_score": LOW_UTILITY_SCORE,
        },
        "execution": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "model_called": False,
            "automatic_lifecycle_action": False,
            "auto_apply": False,
        },
    }
    return {**core, "preview_digest": _digest(core)}


def _row_proposals(row: Mapping[str, Any], *, project: str, report_digest: str) -> list[dict[str, Any]]:
    allowed_fields = {
        "project",
        "memory_id",
        "sample_count",
        "score",
        "uncertainty",
        "event_counts",
        "lifecycle_action",
    }
    unexpected = sorted(str(field) for field in row if str(field) not in allowed_fields)
    if unexpected:
        raise FeedbackConsolidationError("utility report row contains unsupported fields")
    if _required_text(row.get("project"), "utility.row.project") != project:
        raise FeedbackConsolidationError("utility report row project mismatch")
    if str(row.get("lifecycle_action") or "") != "none":
        raise FeedbackConsolidationError("utility report lifecycle_action must be none")
    memory_id = _required_text(row.get("memory_id"), "utility.row.memory_id")
    sample_count = _nonnegative_int(row.get("sample_count"), "utility.row.sample_count")
    score = _score(row.get("score"))
    event_counts = _event_counts(row.get("event_counts"))
    uncertainty = str(row.get("uncertainty") or "").strip()
    if uncertainty not in {"high", "bounded"}:
        raise FeedbackConsolidationError("utility.row.uncertainty is invalid")
    if sample_count < MIN_SUPPORTED_SAMPLES or uncertainty != "bounded":
        return []

    candidates: list[tuple[str, int]] = []
    if event_counts["contradicted"]:
        candidates.append(("contradiction_review", event_counts["contradicted"]))
    if event_counts["corrected"]:
        candidates.append(("correction_review", event_counts["corrected"]))
    if score <= LOW_UTILITY_SCORE:
        candidates.append(("low_utility_review", sample_count))
    return [
        _proposal(
            project=project,
            memory_id=memory_id,
            report_digest=report_digest,
            review_kind=review_kind,
            evidence_count=evidence_count,
            sample_count=sample_count,
            score=score,
        )
        for review_kind, evidence_count in candidates
    ]


def _proposal(
    *,
    project: str,
    memory_id: str,
    report_digest: str,
    review_kind: str,
    evidence_count: int,
    sample_count: int,
    score: float,
) -> dict[str, Any]:
    canonical = {
        "project": project,
        "memory_id": memory_id,
        "source_utility_report_digest": report_digest,
        "review_kind": review_kind,
    }
    return {
        "proposal_id": _digest(canonical),
        **canonical,
        "evidence_count": evidence_count,
        "sample_count": sample_count,
        "utility_score": score,
        "status": "operator_review_required",
        "recommended_action": "review_authoritative_evidence",
        "required_gates": (
            "same_snapshot_recheck",
            "content_free_evidence_review",
            "explicit_operator_approval",
            "typed_dry_run_before_any_mutation",
            "post_apply_parity_smoke",
        ),
        "lifecycle_action": "none",
        "apply_performed": False,
    }


def _event_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise FeedbackConsolidationError("utility.row.event_counts must be an object")
    result: dict[str, int] = {}
    for name in ("contradicted", "corrected"):
        result[name] = _nonnegative_int(value.get(name, 0), f"utility.row.event_counts.{name}")
    return result


def _nonnegative_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FeedbackConsolidationError(f"{field_name} must be an integer") from exc
    if parsed < 0:
        raise FeedbackConsolidationError(f"{field_name} must be non-negative")
    return parsed


def _score(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise FeedbackConsolidationError("utility.row.score must be numeric") from exc
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise FeedbackConsolidationError("utility.row.score must be finite")
    return round(parsed, 6)


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 160:
        raise FeedbackConsolidationError(f"{field_name} must be a non-empty bounded string")
    return text


def _required_digest(value: Any, field_name: str) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise FeedbackConsolidationError(f"{field_name} must be a SHA-256 digest")
    return digest


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "FeedbackConsolidationError",
    "SCHEMA_VERSION",
    "build_feedback_consolidation_preview",
]
