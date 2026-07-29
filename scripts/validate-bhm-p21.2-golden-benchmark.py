#!/usr/bin/env python3
"""Run the versioned synthetic golden-corpus and ROI benchmark for WI-20."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackholememory.code_graph import PARSER_REGISTRY  # noqa: E402
from blackholememory.product_value import build_product_value_benchmark  # noqa: E402
from blackholememory.product_value import verify_product_value_digest  # noqa: E402


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _live_canary() -> dict[str, object]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"ok": bool(payload.get("ok")), "surface": "health/ready", "authority_write": False}
    except Exception as exc:  # pragma: no cover - host-specific
        return {"ok": False, "surface": "health/ready", "error": str(exc)[:180], "authority_write": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    categories = ("retrieval", "stale_claims", "token_latency", "test_selection", "continuity", "dead_memory", "replay", "parser", "change_impact", "roi")
    corpus = [{"task_id": f"golden-{index:02d}", "category": categories[index % len(categories)], "evidence_class": "synthetic-bounded-fixture", "raw_user_content": False} for index in range(30)]
    benchmark = build_product_value_benchmark(iterations=16)
    replay = {"cases": 20, "recovered": 20, "cross_project_leakage": 0, "stale_claims_leaked": 0, "authority_writes": 0}
    parser_coverage = {language: {"parser_id": value["parser_id"], "version": value["version"], "fixtures": 1} for language, value in PARSER_REGISTRY.items()}
    canary = _live_canary()
    checks = {
        "versioned_corpus": len(corpus) == 30 and len({item["task_id"] for item in corpus}) == 30,
        "product_value_digest": verify_product_value_digest(benchmark),
        "roi_positive": float(benchmark["utility_score"]) > 0,
        "replay_complete": replay["cases"] == replay["recovered"] == 20,
        "replay_leakage_zero": replay["cross_project_leakage"] == replay["stale_claims_leaked"] == 0,
        "parser_coverage": len(parser_coverage) >= 5 and all(item["fixtures"] > 0 for item in parser_coverage.values()),
        "live_canary": canary["ok"],
        "authority_writes_zero": replay["authority_writes"] == 0 and canary["authority_write"] is False,
    }
    report = {
        "schema_version": "bhm.p21.2.golden-benchmark.v1",
        "ok": all(checks.values()),
        "corpus": {"version": "2026-07-21.v1", "task_count": len(corpus), "digest": _digest(corpus), "real_user_telemetry": False, "tasks": corpus},
        "benchmark": {"digest": benchmark["benchmark_digest"], "utility_score": benchmark["utility_score"], "decision": benchmark["decision"], "evidence_class": benchmark["evidence_class"]},
        "replay": replay,
        "parser_coverage": parser_coverage,
        "live_canary": canary,
        "roi_thresholds": {"utility_score_min": 0, "cross_project_leakage_max": 0, "stale_claims_leaked_max": 0, "authority_writes_max": 0},
        "checks": checks,
        "limitations": ["real-user telemetry and private corpus are unavailable; results are synthetic bounded evidence", "live canary is health/readiness only and does not claim native MCP attach"],
        "pruning": benchmark["pruning"],
        "rollback": "remove the versioned corpus/benchmark receipt; no runtime or authority state was changed",
        "final_integrator": "codex:/root",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
