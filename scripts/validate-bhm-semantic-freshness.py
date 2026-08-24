#!/usr/bin/env python
"""Build a metadata-only semantic-search freshness/provider-SLO receipt.

WI-108 closes the evidence gap left by the bounded WI-82/WI-97 probes.  The
validator consumes already captured runtime/search metadata and never calls a
provider, starts a model, toggles ``BHM_CODE_SEMANTIC_FUSION`` or writes any
state.  Missing provider-SLO observations remain an explicit gap; they are
never converted into a PASS by assuming a healthy provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "bhm.p28.wi108.semantic-freshness.v1"
DEFAULT_MAX_SNAPSHOT_AGE_SECONDS = 86_400.0
DEFAULT_PROVIDER_P95_BUDGET_MS = 5_000.0
DEFAULT_PROVIDER_ERROR_RATE = 0.0


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_text(value: Any, limit: int = 240) -> str:
    return str(value or "").replace("\x00", "")[:limit]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _execution_safe(payload: Mapping[str, Any]) -> bool:
    """Require explicit false write/model/source markers when present."""

    execution = payload.get("execution") if isinstance(payload.get("execution"), Mapping) else {}
    forbidden = ("writes_sqlite_state", "writes_qdrant", "model_started", "autonomous_apply", "raw_source_returned")
    return all(execution.get(key) is not True for key in forbidden)


def build_freshness_receipt(
    evidence: Mapping[str, Any],
    *,
    max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    provider_p95_budget_ms: float = DEFAULT_PROVIDER_P95_BUDGET_MS,
    provider_error_rate_budget: float = DEFAULT_PROVIDER_ERROR_RATE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate captured freshness/provider metadata without side effects.

    ``evidence`` is normally the merged output of WI-82's ``runtime``,
    ``freshness`` and ``semantic`` sections.  ``provider_slo`` is optional but
    required for a production-quality PASS; its absence is reported as a
    recoverable evidence gap.  The function only reads bounded scalar fields
    and never consumes source text or vectors.
    """

    runtime = evidence.get("runtime") if isinstance(evidence.get("runtime"), Mapping) else {}
    freshness = evidence.get("freshness") if isinstance(evidence.get("freshness"), Mapping) else {}
    semantic = evidence.get("semantic") if isinstance(evidence.get("semantic"), Mapping) else {}
    provider = runtime.get("provider") if isinstance(runtime.get("provider"), Mapping) else {}
    slo = runtime.get("slo") if isinstance(runtime.get("slo"), Mapping) else {}
    rows = semantic.get("queries") if isinstance(semantic.get("queries"), list) else []
    gaps: list[str] = []
    failures: list[str] = []

    runtime_ok = bool(runtime.get("ok")) and _execution_safe(runtime)
    if not runtime_ok:
        failures.append("runtime_not_ready_or_execution_boundary_failed")
    freshness_ok = bool(freshness.get("ok"))
    if not freshness_ok:
        failures.append("index_or_graph_freshness_failed")
    age = _number(freshness.get("snapshot_age_seconds"))
    age_budget = max(1.0, _number(max_snapshot_age_seconds) or DEFAULT_MAX_SNAPSHOT_AGE_SECONDS)
    age_ok = age is not None and age <= age_budget
    if not age_ok:
        failures.append("snapshot_age_outside_budget")

    provider_ready = bool(provider.get("ok")) and bool(provider.get("provider_ready")) and bool(provider.get("qdrant_healthy"))
    if not provider_ready:
        failures.append("provider_or_qdrant_not_ready")
    slo_ok = bool(slo.get("ok")) and str(slo.get("status") or "").casefold() == "healthy"
    if not slo_ok:
        failures.append("runtime_slo_not_healthy")

    projection_only = _execution_safe(semantic) and all(
        bool(row.get("projection_only")) for row in rows if isinstance(row, Mapping)
    )
    if not projection_only:
        failures.append("semantic_receipt_not_projection_only")
    semantic_state = _bounded_text(semantic.get("state"), 32).casefold() or "unknown"
    semantic_evaluated = bool(semantic.get("evaluated"))
    if not semantic_evaluated:
        gaps.append("semantic_relevance_not_evaluated")
    if semantic_state in {"error", "unavailable"}:
        failures.append(f"semantic_state_{semantic_state}")

    provider_slo = evidence.get("provider_slo") if isinstance(evidence.get("provider_slo"), Mapping) else {}
    request_count = _number(provider_slo.get("request_count"))
    error_count = _number(provider_slo.get("error_count"))
    p95_ms = _number(provider_slo.get("p95_latency_ms"))
    p95_budget = max(1.0, _number(provider_p95_budget_ms) or DEFAULT_PROVIDER_P95_BUDGET_MS)
    error_budget = max(0.0, min(1.0, _number(provider_error_rate_budget) if _number(provider_error_rate_budget) is not None else DEFAULT_PROVIDER_ERROR_RATE))
    provider_slo_evaluated = request_count is not None and request_count > 0 and error_count is not None and p95_ms is not None
    if not provider_slo_evaluated:
        gaps.append("provider_slo_observation_missing")
    error_rate = None
    error_rate_ok = False
    latency_ok = False
    if provider_slo_evaluated:
        error_rate = max(0.0, error_count) / max(request_count, 1.0)
        error_rate_ok = error_rate <= error_budget
        latency_ok = p95_ms >= 0.0 and p95_ms <= p95_budget
        if not error_rate_ok:
            failures.append("provider_error_rate_budget_exceeded")
        if not latency_ok:
            failures.append("provider_p95_latency_budget_exceeded")

    checks = {
        "runtime_ready": runtime_ok,
        "snapshot_fresh": bool(freshness_ok and age_ok),
        "provider_ready": provider_ready,
        "runtime_slo_healthy": slo_ok,
        "projection_only": projection_only,
        "semantic_relevance_evaluated": semantic_evaluated,
        "provider_slo_evaluated": provider_slo_evaluated,
        "provider_error_rate_within_budget": error_rate_ok,
        "provider_p95_within_budget": latency_ok,
    }
    if failures:
        status = "fail"
    elif gaps:
        status = "gap"
    else:
        status = "pass"
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "ok": status == "pass",
        "checks": checks,
        "gaps": sorted(set(gaps)),
        "failures": sorted(set(failures)),
        "freshness": {
            "snapshot_age_seconds": age,
            "max_snapshot_age_seconds": round(age_budget, 3),
            "snapshot_graph_aligned": bool(freshness.get("snapshot_graph_aligned")),
            "snapshot_digest": _bounded_text(freshness.get("snapshot_digest"), 128),
            "graph_snapshot_id": _bounded_text(freshness.get("graph_snapshot_id"), 128),
        },
        "provider_slo": {
            "evaluated": provider_slo_evaluated,
            "request_count": None if request_count is None else int(request_count),
            "error_count": None if error_count is None else int(error_count),
            "error_rate": None if error_rate is None else round(error_rate, 8),
            "error_rate_budget": round(error_budget, 8),
            "p95_latency_ms": None if p95_ms is None else round(p95_ms, 3),
            "p95_latency_budget_ms": round(p95_budget, 3),
        },
        "semantic": {
            "state": semantic_state,
            "evaluated": semantic_evaluated,
            "query_count": len(rows),
            "active_queries": int(semantic.get("active_queries") or 0),
            "embedding_contract": semantic.get("embedding_contract") if isinstance(semantic.get("embedding_contract"), Mapping) else {},
        },
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "model_started": False,
            "autonomous_apply": False,
            "raw_source_returned": False,
            "network_writes": False,
        },
    }
    receipt["evidence_digest"] = _sha256({key: value for key, value in receipt.items() if key != "evidence_digest"})
    return receipt


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("input must be a JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="captured WI-82 metadata JSON; no network calls are made")
    parser.add_argument("--max-snapshot-age-seconds", type=float, default=DEFAULT_MAX_SNAPSHOT_AGE_SECONDS)
    parser.add_argument("--provider-p95-budget-ms", type=float, default=DEFAULT_PROVIDER_P95_BUDGET_MS)
    parser.add_argument("--provider-error-rate-budget", type=float, default=DEFAULT_PROVIDER_ERROR_RATE)
    args = parser.parse_args()
    receipt = build_freshness_receipt(
        _load(args.input),
        max_snapshot_age_seconds=args.max_snapshot_age_seconds,
        provider_p95_budget_ms=args.provider_p95_budget_ms,
        provider_error_rate_budget=args.provider_error_rate_budget,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
