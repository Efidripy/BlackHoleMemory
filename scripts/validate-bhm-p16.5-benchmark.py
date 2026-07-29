"""Run the deterministic P16.5 retrieval quality/leakage/budget benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from blackholememory.context_compiler import compile_context  # noqa: E402
from blackholememory.retrieval_benchmark import build_default_benchmark_cases  # noqa: E402
from blackholememory.retrieval_benchmark import evaluate_benchmark  # noqa: E402
from blackholememory.retrieval_benchmark import filter_benchmark_hits  # noqa: E402


BASELINE_BUDGET = 1200
TUNED_BUDGET = 350
MIN_COST_REDUCTION = 0.20
STRESS_SUFFIX = " canonical evidence validated architecture contract" * 80


def build_stress_cases(count: int = 120):
    cases = []
    for case in build_default_benchmark_cases(count):
        hits = []
        for hit in case.hits:
            stressed = dict(hit)
            stressed["content"] = str(hit.get("content") or "") + STRESS_SUFFIX
            hits.append(stressed)
        cases.append(replace(case, hits=tuple(hits)))
    return cases


def score_ranker(_query: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(hits, key=lambda hit: float(hit.get("score") or 0.0), reverse=True)


def compile_metrics(cases, *, token_budget: int) -> dict[str, float]:
    estimated_tokens: list[int] = []
    included_counts: list[int] = []
    relevant_citations = 0
    for index, case in enumerate(cases):
        filtered = filter_benchmark_hits(case.hits, project=case.project)
        ranked = score_ranker(case.query, [dict(hit, metadata=dict(hit.get("metadata") or {})) for hit in filtered])
        context_items = [
            {
                "id": str(hit.get("metadata", {}).get("source_id") or hit.get("id") or ""),
                "title": hit.get("metadata", {}).get("raw_title") or str(hit.get("id") or ""),
                "project": hit.get("metadata", {}).get("project"),
                "content": str(hit.get("content") or ""),
                "score": float(hit.get("score") or 0.0),
                "context_origin": hit.get("context_origin") or "LOCAL",
            }
            for hit in ranked[:5]
        ]
        compiled = compile_context(context_items, token_budget=token_budget)
        estimated_tokens.append(int(compiled["estimated_tokens"]))
        included_counts.append(int(compiled["included_count"]))
        relevant_id = f"relevant-{index}"
        relevant_citations += int(any(citation.get("id") == relevant_id for citation in compiled["citations"]))
    total = max(len(cases), 1)
    return {
        "average_estimated_tokens": round(sum(estimated_tokens) / total, 6),
        "max_estimated_tokens": max(estimated_tokens, default=0),
        "average_included_count": round(sum(included_counts) / total, 6),
        "relevant_citation_rate": round(relevant_citations / total, 6),
        "token_budget": token_budget,
    }


def run_gate(*, count: int = 120) -> dict[str, Any]:
    cases = build_stress_cases(count)
    baseline_quality = evaluate_benchmark(cases, ranker=score_ranker, token_budget=BASELINE_BUDGET)
    tuned_quality = evaluate_benchmark(cases, ranker=score_ranker, token_budget=TUNED_BUDGET)
    baseline_metrics = compile_metrics(cases, token_budget=BASELINE_BUDGET)
    tuned_metrics = compile_metrics(cases, token_budget=TUNED_BUDGET)
    quality_fields = ("top1_accuracy", "ndcg_at_5", "filter_correctness", "leakage_count")
    quality_equal = all(baseline_quality[field] == tuned_quality[field] for field in quality_fields)
    quality_equal = quality_equal and baseline_metrics["relevant_citation_rate"] == tuned_metrics["relevant_citation_rate"]
    reduction = 1.0 - tuned_metrics["average_estimated_tokens"] / max(baseline_metrics["average_estimated_tokens"], 1.0)
    result = {
        "schema_version": 1,
        "cases": count,
        "baseline": {"quality": baseline_quality, "context": baseline_metrics},
        "tuned": {"quality": tuned_quality, "context": tuned_metrics},
        "token_cost_reduction": round(reduction, 6),
        "quality_equal": quality_equal,
        "leakage_free": baseline_quality["leakage_count"] == 0 and tuned_quality["leakage_count"] == 0,
    }
    result["ok"] = bool(
        baseline_quality["ok"]
        and tuned_quality["ok"]
        and quality_equal
        and result["leakage_free"]
        and reduction >= MIN_COST_REDUCTION
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=120)
    args = parser.parse_args()
    if not 100 <= args.cases <= 200:
        parser.error("--cases must be between 100 and 200")
    result = run_gate(count=args.cases)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
