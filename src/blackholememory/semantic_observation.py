"""Metadata-only observation for opt-in semantic code-search fusion.

This surface deliberately reports freshness and evidence gaps instead of
pretending that a healthy Qdrant projection proves broad semantic relevance.
It consumes an already indexed repository snapshot and request metadata; it
never calls an embedding provider, writes state, returns source, or toggles
``BHM_CODE_SEMANTIC_FUSION``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping


SEMANTIC_OBSERVATION_SCHEMA_VERSION = "bhm.semantic-observation.v1"
SEMANTIC_FRESHNESS_RECEIPT_SCHEMA_VERSION = "bhm.semantic-freshness-receipt.v1"
DEFAULT_MAX_SNAPSHOT_AGE_SECONDS = 86_400.0
DEFAULT_SEMANTIC_LATENCY_BUDGET_MS = 1_000.0


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_semantic_freshness_receipt(
    snapshot: Mapping[str, Any],
    *,
    requested: bool,
    feature_enabled: bool,
    request_status: str,
    active: bool,
    provider_ready: bool | None = None,
    runtime_slo_status: str | None = None,
    graph_snapshot_id: str | None = None,
    graph_digest: str | None = None,
    observed_latency_ms: float | None = None,
    latency_budget_ms: float = DEFAULT_SEMANTIC_LATENCY_BUDGET_MS,
    now: datetime | None = None,
    max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
) -> dict[str, Any]:
    """Build a deterministic, metadata-only semantic freshness receipt.

    The receipt is deliberately observational: it never starts a provider,
    changes the feature flag, writes a projection or claims semantic quality.
    Missing runtime/provider/latency observations remain explicit gaps.
    """

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    completed = _parse_timestamp(snapshot.get("completed_at"))
    age: float | None = None
    if completed is not None:
        age = max(0.0, (current_time - completed).total_seconds())
    age_budget = max(1.0, float(max_snapshot_age_seconds))
    latency_budget = max(1.0, float(latency_budget_ms))
    if age is None:
        freshness_status = "unknown"
    elif age > age_budget:
        freshness_status = "stale"
    else:
        freshness_status = "fresh"

    normalized_request = str(request_status or "unknown").strip().casefold() or "unknown"
    provider_status = "ready" if provider_ready is True else "unavailable" if normalized_request == "unavailable" else "unknown"
    runtime_status = str(runtime_slo_status or "unknown").strip().casefold() or "unknown"
    if runtime_status not in {"healthy", "breached", "unknown"}:
        runtime_status = "unknown"
    latency_status = "unknown"
    normalized_latency: float | None = None
    if observed_latency_ms is not None:
        try:
            normalized_latency = round(max(0.0, float(observed_latency_ms)), 3)
            latency_status = "breached" if normalized_latency > latency_budget else "within_budget"
        except (TypeError, ValueError, OverflowError):
            latency_status = "unknown"

    gaps: list[str] = []
    failures: list[str] = []
    if requested and not feature_enabled:
        status = "disabled"
    elif not requested:
        status = "not_requested"
    else:
        if freshness_status == "stale":
            status = "stale"
            failures.append("snapshot_age_outside_budget")
        elif freshness_status == "unknown":
            status = "gap"
            gaps.append("snapshot_completed_at_missing")
        elif provider_status == "unavailable" or runtime_status == "breached" or latency_status == "breached":
            status = "fail"
            if provider_status == "unavailable":
                failures.append("semantic_provider_unavailable")
            if runtime_status == "breached":
                failures.append("runtime_slo_breached")
            if latency_status == "breached":
                failures.append("semantic_latency_budget_exceeded")
        else:
            status = "fresh"
            if provider_status == "unknown":
                gaps.append("provider_readiness_observation_missing")
            if runtime_status == "unknown":
                gaps.append("runtime_slo_observation_missing")
            if latency_status == "unknown":
                gaps.append("semantic_latency_observation_missing")
            if not active:
                gaps.append("semantic_relevance_not_evaluated")
            if gaps:
                status = "gap"

    receipt: dict[str, Any] = {
        "schema_version": SEMANTIC_FRESHNESS_RECEIPT_SCHEMA_VERSION,
        "status": status,
        "feature_flag": {
            "name": "BHM_CODE_SEMANTIC_FUSION",
            "requested": bool(requested),
            "enabled": bool(feature_enabled),
        },
        "freshness": {
            "status": freshness_status,
            "snapshot_age_seconds": None if age is None else round(age, 3),
            "max_snapshot_age_seconds": round(age_budget, 3),
            "snapshot_completed_at": str(snapshot.get("completed_at") or "")[:64],
            "snapshot_digest": str(snapshot.get("snapshot_digest") or "")[:128],
        },
        "provider": {
            "status": provider_status,
            "request_status": normalized_request,
            "projection_only": True,
        },
        "runtime": {
            "slo_status": runtime_status,
            "graph_snapshot_id": str(graph_snapshot_id or "")[:128],
            "graph_digest": str(graph_digest or "")[:128],
            "graph_bound": bool(graph_snapshot_id or graph_digest),
        },
        "latency_slo": {
            "status": latency_status,
            "observed_latency_ms": normalized_latency,
            "budget_ms": round(latency_budget, 3),
        },
        "active": bool(active),
        "gaps": sorted(set(gaps)),
        "failures": sorted(set(failures)),
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "model_started": False,
            "network": False,
            "raw_source_returned": False,
        },
    }
    receipt["evidence_digest"] = _digest({key: value for key, value in receipt.items() if key != "evidence_digest"})
    return receipt


def build_semantic_observation(
    snapshot: Mapping[str, Any],
    *,
    requested: bool,
    request_status: str,
    active: bool,
    feature_enabled: bool = True,
    provider_ready: bool | None = None,
    runtime_slo_status: str | None = None,
    graph_snapshot_id: str | None = None,
    graph_digest: str | None = None,
    observed_latency_ms: float | None = None,
    latency_budget_ms: float = DEFAULT_SEMANTIC_LATENCY_BUDGET_MS,
    now: datetime | None = None,
    max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
) -> dict[str, Any]:
    """Build a deterministic freshness/evidence observation.

    ``status`` is ``pass`` only for a fresh snapshot with an enabled request;
    missing provider SLO and relevance labels remain explicit gaps.  A
    disabled/not-requested call is reported as ``not_requested`` and never
    upgraded to a semantic quality claim.
    """

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    completed = _parse_timestamp(snapshot.get("completed_at"))
    age: float | None = None
    if completed is not None:
        age = max(0.0, (current_time - completed).total_seconds())
    budget = max(1.0, float(max_snapshot_age_seconds))
    gaps: list[str] = []
    failures: list[str] = []
    if age is None:
        freshness = "unknown"
        gaps.append("snapshot_completed_at_missing")
    elif age > budget:
        freshness = "stale"
        failures.append("snapshot_age_outside_budget")
    else:
        freshness = "fresh"
    normalized_status = str(request_status or "unknown").strip().casefold() or "unknown"
    if not requested:
        observation_status = "not_requested"
    elif normalized_status == "unavailable":
        observation_status = "fail"
        failures.append("semantic_provider_unavailable")
    elif failures:
        observation_status = "fail"
    else:
        observation_status = "gap"
        gaps.extend(("provider_slo_observation_missing", "semantic_relevance_not_evaluated"))

    result: dict[str, Any] = {
        "schema_version": SEMANTIC_OBSERVATION_SCHEMA_VERSION,
        "status": observation_status,
        "requested": bool(requested),
        "request_status": normalized_status,
        "active": bool(active),
        "freshness": {
            "status": freshness,
            "snapshot_age_seconds": None if age is None else round(age, 3),
            "max_snapshot_age_seconds": round(budget, 3),
            "snapshot_completed_at": str(snapshot.get("completed_at") or "")[:64],
            "snapshot_digest": str(snapshot.get("snapshot_digest") or "")[:128],
        },
        "gaps": sorted(set(gaps)),
        "failures": sorted(set(failures)),
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "model_started": False,
            "network": False,
            "raw_source_returned": False,
        },
    }
    result["freshness_receipt"] = build_semantic_freshness_receipt(
        snapshot,
        requested=requested,
        feature_enabled=feature_enabled,
        request_status=normalized_status,
        active=active,
        provider_ready=provider_ready,
        runtime_slo_status=runtime_slo_status,
        graph_snapshot_id=graph_snapshot_id,
        graph_digest=graph_digest,
        observed_latency_ms=observed_latency_ms,
        latency_budget_ms=latency_budget_ms,
        now=current_time,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
    )
    result["evidence_digest"] = _digest({key: value for key, value in result.items() if key != "evidence_digest"})
    return result


__all__ = [
    "DEFAULT_MAX_SNAPSHOT_AGE_SECONDS",
    "DEFAULT_SEMANTIC_LATENCY_BUDGET_MS",
    "SEMANTIC_FRESHNESS_RECEIPT_SCHEMA_VERSION",
    "SEMANTIC_OBSERVATION_SCHEMA_VERSION",
    "build_semantic_freshness_receipt",
    "build_semantic_observation",
]
