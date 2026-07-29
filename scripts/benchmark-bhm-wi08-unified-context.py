"""Synthetic WI-08 unified context source-coverage benchmark."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from blackholememory.unified_context import compile_unified_context


def _items(count: int) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for source in ("code", "conventions", "tasks", "docs", "ops", "memory"):
        result[source] = [
            {
                "id": f"{source}-{index}",
                "title": f"{source} item {index}",
                "content": f"Bounded {source} evidence item {index} for the unified context benchmark.",
                "source_refs": [f"{source}/evidence-{index}.md#L1"],
                "files": [f"{source}/evidence-{index}.md"],
                "metadata": {"source_kind": source},
            }
            for index in range(count)
        ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items-per-source", type=int, default=20)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--p95-budget-ms", type=float, default=250.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    sources = _items(max(1, args.items_per_source))
    durations: list[float] = []
    digests: list[str] = []
    first = None
    for _ in range(max(1, args.iterations)):
        started = time.perf_counter()
        result = compile_unified_context(sources, project="benchmark", query="unified", token_budget=1_200, max_items_per_source=16)
        durations.append((time.perf_counter() - started) * 1_000)
        digests.append(result["response_digest"])
        first = result
    p95 = statistics.quantiles(durations, n=20, method="inclusive")[18] if len(durations) >= 2 else durations[0]
    checks = {
        "source_coverage": all(first["sources"]["requested"].get(source, 0) == min(args.items_per_source, 16) for source in first["sources"]["requested"]),
        "provenance_complete": first["provenance"]["complete"] is True,
        "deterministic_digest": len(set(digests)) == 1,
        "p95_budget": p95 <= args.p95_budget_ms,
        "bounded_context": first["estimated_tokens"] <= first["token_budget"],
        "no_writes": first["execution"]["writes_sqlite_state"] is False and first["execution"]["writes_qdrant"] is False and first["execution"]["model_started"] is False,
    }
    report = {
        "schema_version": "bhm.unified-context.benchmark.v1",
        "ok": all(checks.values()),
        "fixture": {"items_per_source": args.items_per_source, "source_count": 6, "iterations": args.iterations},
        "latency": {"sample_count": len(durations), "p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3)},
        "context": {"estimated_tokens": first["estimated_tokens"], "token_budget": first["token_budget"], "truncated": first["truncated"], "response_digest": first["response_digest"]},
        "checks": checks,
        "writes_live_state": False,
        "writes_qdrant": False,
        "model_started": False,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        output = Path(args.report).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
