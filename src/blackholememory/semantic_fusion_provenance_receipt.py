"""Bounded provenance receipt for public semantic code-search fusion."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


SEMANTIC_FUSION_PROVENANCE_RECEIPT_SCHEMA_VERSION = "bhm.semantic-fusion.provenance-receipt.v1"
MAX_PROVENANCE_ITEMS = 128


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
    return "unknown:" + _digest({key: value for key, value in item.items() if key not in {"content", "snippet", "vector", "embedding"}})[:32]


def _identity_digest(items: Sequence[Mapping[str, Any]]) -> str:
    identities: list[str] = []
    for item in list(items)[:MAX_PROVENANCE_ITEMS]:
        identity = _identity(item)
        if identity not in identities:
            identities.append(identity)
    return _digest(identities)


def _bounded_dimensions(value: Any) -> int:
    try:
        return max(0, min(int(value or 0), 65_536))
    except (TypeError, ValueError, OverflowError):
        return 0


def build_semantic_fusion_provenance_receipt(
    *,
    embedding_contract: Mapping[str, Any] | None,
    baseline_matches: Sequence[Mapping[str, Any]],
    fused_matches: Sequence[Mapping[str, Any]],
    semantic_hits: int,
    requested: bool,
    feature_enabled: bool,
    active: bool,
    request_status: str,
    snapshot_digest: str | None = None,
    graph_snapshot_id: str | None = None,
    graph_digest: str | None = None,
    semantic_weight: float = 0.35,
) -> dict[str, Any]:
    """Build a deterministic rank/provenance receipt without vectors or source."""

    contract = embedding_contract if isinstance(embedding_contract, Mapping) else {}
    contract_view = {
        "schema_version": str(contract.get("schema_version") or "")[:128],
        "provider": str(contract.get("provider") or "")[:128],
        "model_digest": str(contract.get("model_digest") or "")[:128],
        "dimensions": _bounded_dimensions(contract.get("dimensions")),
        "feature_flag": str(contract.get("feature_flag") or "")[:128],
        "authority": str(contract.get("authority") or "")[:128],
    }
    request = str(request_status or "unknown").strip().casefold()[:32] or "unknown"
    graph_bound = bool(str(graph_snapshot_id or "").strip() and str(graph_digest or "").strip())
    if not requested:
        status = "not_requested"
    elif requested and not feature_enabled:
        status = "disabled"
    elif request in {"unavailable", "failed", "blocked"}:
        status = "blocked"
    elif not active:
        status = "gap"
    elif not contract_view["model_digest"] or not contract_view["dimensions"] or not graph_bound:
        status = "gap"
    else:
        status = "observed"
    return_data = {
        "schema_version": SEMANTIC_FUSION_PROVENANCE_RECEIPT_SCHEMA_VERSION,
        "status": status,
        "proposal_only": True,
        "embedding": {
            **contract_view,
            "contract_digest": _digest(contract_view),
            "vectors_returned": False,
        },
        "binding": {
            "snapshot_digest": str(snapshot_digest or "")[:128],
            "graph_snapshot_id": str(graph_snapshot_id or "")[:128],
            "graph_digest": str(graph_digest or "")[:128],
            "graph_bound": graph_bound,
        },
        "coverage": {
            "semantic_hit_count": max(0, min(int(semantic_hits), MAX_PROVENANCE_ITEMS)),
            "baseline_count": min(len(baseline_matches), MAX_PROVENANCE_ITEMS),
            "fused_count": min(len(fused_matches), MAX_PROVENANCE_ITEMS),
            "baseline_identity_digest": _identity_digest(baseline_matches),
            "fused_identity_digest": _identity_digest(fused_matches),
            "rank_only": True,
        },
        "ranking": {"semantic_weight": round(max(0.0, min(float(semantic_weight), 0.75)), 6)},
        "gaps": sorted(
            {
                *( ["embedding_contract_missing"] if not contract_view["model_digest"] or not contract_view["dimensions"] else [] ),
                *( ["graph_binding_missing"] if not graph_bound else [] ),
                *( ["semantic_results_missing"] if not fused_matches else [] ),
            }
        ),
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "provider_called": False,
            "network": False,
            "raw_source_returned": False,
            "raw_snippets_returned": False,
            "embedding_vectors_returned": False,
            "autonomous_apply": False,
        },
    }
    return {**return_data, "evidence_digest": _digest(return_data)}


__all__ = [
    "SEMANTIC_FUSION_PROVENANCE_RECEIPT_SCHEMA_VERSION",
    "build_semantic_fusion_provenance_receipt",
]
