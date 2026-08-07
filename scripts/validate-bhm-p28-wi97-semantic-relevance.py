"""Bounded offline semantic relevance and latency/error-budget benchmark.

This is an evidence fixture, not a provider or production-default switch. It
uses the local rank-fusion function with labelled metadata-only cases and
measures the real local CPU path. No model, network, SQLite or Qdrant writes
are allowed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from statistics import quantiles
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.code_search import fuse_code_search_matches  # noqa: E402
from blackholememory.filesystem_boundaries import replace_bytes_safely  # noqa: E402

SCHEMA_VERSION = "bhm.p28.wi97.semantic-relevance.v1"
MAX_CASES = 32


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def run(cases: int = 16) -> dict[str, Any]:
    count = max(1, min(int(cases), MAX_CASES))
    rows: list[dict[str, Any]] = []
    previous_flag = os.environ.get("BHM_CODE_SEMANTIC_FUSION")
    os.environ["BHM_CODE_SEMANTIC_FUSION"] = "1"
    try:
      for index in range(count):
        target = f"src/target_{index:02d}.py"
        distractor = f"src/distractor_{index:02d}.py"
        started = time.perf_counter()
        fused = fuse_code_search_matches(
            [
                {"path": distractor, "language": "python", "score": 0.91, "match_kind": "metadata"},
                {"path": target, "language": "python", "score": 0.90, "match_kind": "metadata"},
            ],
            [
                {"path": target, "score": 0.99, "metadata": {"source_id": target}},
                {"path": distractor, "score": 0.10, "metadata": {"source_id": distractor}},
            ],
            limit=2,
            semantic_weight=0.7,
        )
        latency_ms = (time.perf_counter() - started) * 1_000.0
        top = str(fused[0].get("path") or "") if fused else ""
        rows.append(
            {
                "case_id": f"relevance_{index:02d}",
                "expected_top_path": target,
                "observed_top_path": top,
                "top1_correct": top == target,
                "metadata_only": all("content" not in item and "snippet" not in item for item in fused),
                "latency_ms": round(latency_ms, 3),
            }
        )
    finally:
        if previous_flag is None:
            os.environ.pop("BHM_CODE_SEMANTIC_FUSION", None)
        else:
            os.environ["BHM_CODE_SEMANTIC_FUSION"] = previous_flag
    latencies = sorted(float(row["latency_ms"]) for row in rows)
    p95 = quantiles(latencies, n=20, method="inclusive")[18] if len(latencies) > 1 else latencies[0]
    failures = sum(not bool(row["top1_correct"]) or not bool(row["metadata_only"]) for row in rows)
    result = {
        "schema_version": SCHEMA_VERSION,
        "cases": count,
        "labelled_relevance": {"top1_correct": count - failures, "top1_accuracy": round((count - failures) / count, 6)},
        "latency_ms": {"p50": latencies[len(latencies) // 2], "p95": round(p95, 3), "max": max(latencies)},
        "error_budget": {"allowed_failures": 0, "observed_failures": failures, "burn_ratio": round(failures / count, 6), "within_budget": failures == 0},
        "provider_calls": 0,
        "model_started": False,
        "feature_flag_default": False,
        "writes_sqlite_state": False,
        "writes_qdrant": False,
        "raw_source_returned": False,
        "rows": rows,
    }
    result["digest"] = _digest({key: value for key, value in result.items() if key not in {"digest", "latency_ms", "rows"}})
    result["ok"] = bool(failures == 0 and result["error_budget"]["within_budget"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=16)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.cases)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        replace_bytes_safely(args.output, payload.encode("utf-8"))
    print(payload, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
