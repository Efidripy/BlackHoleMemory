"""Deterministic WI-177 resolution-quality receipt benchmark."""

from __future__ import annotations

import hashlib
import json
import statistics
import time

from blackholememory.resolution_quality_receipt import build_resolution_quality_receipt


TYPE_RESULT = {
    "proposals": [
        {"relation_kind": "inherits", "unresolved": False, "target_node_id": "n2"},
        {"relation_kind": "package_symbol_reference", "unresolved": True, "target_node_id": "n3"},
        {"relation_kind": "import_reference", "unresolved": True, "target_node_id": ""},
    ],
    "limits": {"max_items": 16},
}
PACKAGE_RESULT = {
    "manifests": [{"path": "pyproject.toml", "bounded_skip": None}],
    "packages": [{"name": "demo", "ecosystem": "python"}],
    "resolution_receipt": {"summary": {"resolved_count": 1, "ambiguous_count": 1, "unresolved_count": 0}},
}
DEPENDENCY_RESULT = {
    "lockfiles": [{"path": "poetry.lock", "bounded_skip": None}],
    "summary": {"status": "resolved", "dependency_count": 3, "unresolved_count": 0},
}


def main() -> None:
    samples: list[float] = []
    receipt = None
    for _ in range(1000):
        started = time.perf_counter_ns()
        receipt = build_resolution_quality_receipt(
            type_result=TYPE_RESULT,
            package_result=PACKAGE_RESULT,
            dependency_result=DEPENDENCY_RESULT,
            graph_snapshot_id="graph-wi177",
            graph_digest="digest-wi177",
            runtime_slo_status="healthy",
        )
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    assert receipt is not None
    digest = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ordered = sorted(samples)
    print(json.dumps({
        "ok": True,
        "iterations": len(samples),
        "p50_ms": round(statistics.median(samples), 4),
        "p95_ms": round(ordered[949], 4),
        "max_ms": round(max(samples), 4),
        "receipt_digest": digest,
        "schema_version": receipt["schema_version"],
        "status": receipt["status"],
        "gaps": receipt["gaps"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
