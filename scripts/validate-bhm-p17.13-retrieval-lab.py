"""Deterministic offline gate for the P17.13 Retrieval Lab preview."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from blackholememory.retrieval_lab import RETRIEVAL_LAB_SCHEMA_VERSION
from blackholememory.retrieval_lab import build_retrieval_lab_preview
from blackholememory.retrieval_lab import verify_retrieval_lab_digest


def main() -> int:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    candidates = [
        {
            "id": "validator-local",
            "content": "retrieval contract implementation evidence and tests",
            "score": 0.92,
            "metadata": {"source_id": "validator-local", "project": "blackholememory", "semantic_type": "feature", "lifecycle": "validated"},
        },
        {
            "id": "validator-cross",
            "content": "retrieval contract from another project",
            "score": 0.99,
            "metadata": {"source_id": "validator-cross", "project": "other-project", "semantic_type": "feature", "lifecycle": "validated"},
        },
    ]
    preview = build_retrieval_lab_preview(
        "retrieval contract",
        project="blackholememory",
        candidates=candidates,
        benchmark_cases=4,
        latency_budget_ms=50,
        observed_latency_ms=12,
        now=now,
    )
    checks = {
        "schema": preview["schema_version"] == RETRIEVAL_LAB_SCHEMA_VERSION,
        "digest": verify_retrieval_lab_digest(preview),
        "query_rewrite": bool(preview["query_rewrites"]),
        "multi_query": len(preview["multi_queries"]) >= 2,
        "hyde": bool(preview["hyde_candidates"]),
        "rerank": bool(preview["reranked"]),
        "hard_negatives": bool(preview["hard_negatives"]),
        "benchmark": len(preview["synthetic_benchmark"]) == 4,
        "leakage_gate": preview["filter_gate"]["leakage_count"] == 1 and not preview["gates"]["leakage"]["passed"],
        "latency_gate": preview["latency_gate"]["passed"] is True,
        "proposal_only": preview["execution"]["model_started"] is False and preview["execution"]["writes_performed"] is False,
    }
    report = {
        "ok": all(checks.values()),
        "schema_version": preview["schema_version"],
        "preview_digest": preview["preview_digest"],
        "summary": preview["summary"],
        "checks": checks,
        "execution_enabled": False,
        "auto_apply": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
