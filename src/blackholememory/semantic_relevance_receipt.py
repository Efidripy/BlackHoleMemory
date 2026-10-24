"""Deterministic metadata-only semantic relevance/freshness receipt.

The receipt compares an existing lexical baseline with an already-produced
metadata-only semantic fusion result.  It reports bounded rank deltas and
evidence-quality buckets without returning source text, embedding vectors,
starting a provider, changing the feature flag, or writing any state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


SEMANTIC_RELEVANCE_RECEIPT_SCHEMA_VERSION = "bhm.semantic-relevance-receipt.v1"
MAX_RELEVANCE_ITEMS = 128


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identity(item: Mapping[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    for candidate in (
        item.get("path"),
        item.get("source_id"),
        metadata.get("path"),
        metadata.get("source_id"),
        metadata.get("upsert_key"),
        item.get("content_sha256"),
    ):
        value = str(candidate or "").strip().replace("\\", "/")
        if value and len(value) <= 512 and "\x00" not in value:
            return value
    return "unknown:" + _digest({"item": {key: value for key, value in item.items() if key not in {"content", "snippet"}}})[:32]


def _ordered_keys(items: Sequence[Mapping[str, Any]]) -> list[str]:
    keys: list[str] = []
    for item in list(items)[:MAX_RELEVANCE_ITEMS]:
        key = _identity(item)
        if key not in keys:
            keys.append(key)
    return keys


def _bounded_status(value: Any, default: str = "unknown") -> str:
    status = str(value or default).strip().casefold()
    return status[:32] or default


def build_semantic_relevance_receipt(
    baseline_matches: Sequence[Mapping[str, Any]],
    fused_matches: Sequence[Mapping[str, Any]],
    *,
    requested: bool,
    feature_enabled: bool,
    request_status: str,
    active: bool,
    provider_ready: bool | None = None,
    graph_snapshot_id: str | None = None,
    graph_digest: str | None = None,
    snapshot_digest: str | None = None,
    runtime_slo_status: str | None = None,
    freshness_receipt: Mapping[str, Any] | None = None,
    semantic_weight: float = 0.35,
) -> dict[str, Any]:
    """Build a bounded, deterministic relevance delta over metadata only.

    ``baseline_matches`` and ``fused_matches`` are treated as opaque result
    metadata.  Only bounded identities/ranks are consumed; content, snippets
    and vectors are never copied into the receipt.
    """

    baseline = _ordered_keys(baseline_matches)
    fused = _ordered_keys(fused_matches)
    baseline_set = set(baseline)
    fused_set = set(fused)
    overlap = baseline_set & fused_set
    baseline_rank = {key: rank for rank, key in enumerate(baseline, start=1)}
    fused_rank = {key: rank for rank, key in enumerate(fused, start=1)}
    moved_up = sum(1 for key in overlap if fused_rank[key] < baseline_rank[key])
    moved_down = sum(1 for key in overlap if fused_rank[key] > baseline_rank[key])
    rank_deltas = [baseline_rank[key] - fused_rank[key] for key in overlap]
    mean_rank_delta = round(sum(rank_deltas) / len(rank_deltas), 3) if rank_deltas else 0.0
    overlap_ratio = round(len(overlap) / max(len(fused), 1), 6)
    top1_changed = bool(baseline and fused and baseline[0] != fused[0])

    request = _bounded_status(request_status)
    provider_status = "ready" if provider_ready is True else "unavailable" if request == "unavailable" else "unknown"
    slo_status = _bounded_status(runtime_slo_status)
    freshness = freshness_receipt if isinstance(freshness_receipt, Mapping) else {}
    freshness_data = freshness.get("freshness") if isinstance(freshness.get("freshness"), Mapping) else {}
    freshness_status = _bounded_status(freshness_data.get("status"))
    graph_bound = bool(str(graph_snapshot_id or "").strip() or str(graph_digest or "").strip())

    gaps: list[str] = []
    failures: list[str] = []
    if requested and not feature_enabled:
        status = "disabled"
    elif not requested:
        status = "not_requested"
    elif provider_status == "unavailable" or provider_ready is False or slo_status == "breached":
        status = "blocked"
        if provider_status == "unavailable" or provider_ready is False:
            failures.append("semantic_provider_unavailable")
        if slo_status == "breached":
            failures.append("runtime_slo_breached")
    elif freshness_status == "stale":
        status = "stale"
        failures.append("snapshot_age_outside_budget")
    elif not active:
        status = "gap"
        gaps.append("semantic_relevance_not_evaluated")
    else:
        status = "observed"
        if provider_status == "unknown":
            gaps.append("provider_readiness_observation_missing")
        if freshness_status == "unknown":
            gaps.append("snapshot_freshness_observation_missing")
        if not graph_bound:
            gaps.append("graph_binding_missing")
        if slo_status == "unknown":
            gaps.append("runtime_slo_observation_missing")

    if not baseline or not fused:
        gaps.append("rank_baseline_or_fused_results_missing")

    if status in {"not_requested", "disabled"}:
        quality_bucket = "not_evaluated"
    elif status in {"blocked", "stale"}:
        quality_bucket = status
    elif not baseline or not fused:
        quality_bucket = "insufficient_evidence"
    elif overlap_ratio >= 0.8 and not top1_changed:
        quality_bucket = "strong_alignment"
    elif overlap_ratio >= 0.5:
        quality_bucket = "mixed_alignment"
    else:
        quality_bucket = "divergent_alignment"

    receipt: dict[str, Any] = {
        "schema_version": SEMANTIC_RELEVANCE_RECEIPT_SCHEMA_VERSION,
        "status": status,
        "proposal_only": True,
        "feature_flag": {
            "name": "BHM_CODE_SEMANTIC_FUSION",
            "requested": bool(requested),
            "enabled": bool(feature_enabled),
            "active": bool(active),
        },
        "provider": {
            "status": provider_status,
            "request_status": request,
            "preexisting_only": True,
            "projection_only": True,
        },
        "baseline": {
            "count": len(baseline),
            "identity_digest": _digest(baseline),
            "authority": "sqlite-authoritative-metadata-search",
        },
        "delta": {
            "fused_count": len(fused),
            "identity_digest": _digest(fused),
            "overlap_count": len(overlap),
            "overlap_ratio": overlap_ratio,
            "top1_changed": top1_changed,
            "moved_up": moved_up,
            "moved_down": moved_down,
            "mean_rank_delta": mean_rank_delta,
            "semantic_weight": round(max(0.0, min(float(semantic_weight), 0.75)), 6),
        },
        "quality": {
            "bucket": quality_bucket,
            "interpretation": "rank-agreement evidence bucket; not a semantic correctness claim",
        },
        "freshness": {
            "status": freshness_status,
            "snapshot_digest": str(snapshot_digest or freshness_data.get("snapshot_digest") or "")[:128],
            "snapshot_age_seconds": freshness_data.get("snapshot_age_seconds"),
        },
        "graph_binding": {
            "snapshot_id": str(graph_snapshot_id or "")[:128],
            "graph_digest": str(graph_digest or "")[:128],
            "bound": graph_bound,
        },
        "slo_binding": {
            "status": slo_status,
            "healthy": slo_status == "healthy",
            "graph_snapshot_id": str(graph_snapshot_id or "")[:128],
            "graph_digest": str(graph_digest or "")[:128],
        },
        "gaps": sorted(set(gaps)),
        "failures": sorted(set(failures)),
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "model_started": False,
            "model_start_policy": "preexisting-provider-only",
            "network": False,
            "raw_source_returned": False,
            "embedding_vectors_returned": False,
            "autonomous_apply": False,
        },
    }
    receipt["evidence_digest"] = _digest({key: value for key, value in receipt.items() if key != "evidence_digest"})
    return receipt


__all__ = ["SEMANTIC_RELEVANCE_RECEIPT_SCHEMA_VERSION", "build_semantic_relevance_receipt"]
