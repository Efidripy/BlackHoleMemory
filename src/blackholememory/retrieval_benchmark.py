"""Offline retrieval/context benchmark helpers with no live-store writes."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .context_compiler import compile_context


DEFAULT_BENCHMARK_CASES = 120
MIN_BENCHMARK_CASES = 100
MAX_BENCHMARK_CASES = 200


@dataclass(frozen=True)
class BenchmarkCase:
    query: str
    project: str
    relevant_ids: frozenset[str]
    hits: tuple[dict[str, Any], ...]


def build_default_benchmark_cases(count: int = DEFAULT_BENCHMARK_CASES) -> list[BenchmarkCase]:
    target = int(count)
    if not MIN_BENCHMARK_CASES <= target <= MAX_BENCHMARK_CASES:
        raise ValueError(f"count must be between {MIN_BENCHMARK_CASES} and {MAX_BENCHMARK_CASES}")

    domains = ("frontend", "backend", "infra", "security", "product", "general")
    semantic_types = ("architecture", "bugfix", "feature", "refactor", "knowledge")
    cases: list[BenchmarkCase] = []
    for index in range(target):
        domain = domains[index % len(domains)]
        semantic_type = semantic_types[(index // len(domains)) % len(semantic_types)]
        project = "blackholememory" if index % 2 == 0 else "e-github-workspace"
        query = f"{domain} {semantic_type} retrieval contract {index}"
        relevant_id = f"relevant-{index}"
        hits = (
            _synthetic_hit(
                relevant_id,
                project=project,
                content=f"{query} canonical decision and validated implementation",
                score=0.95,
                domain=domain,
                semantic_type=semantic_type,
            ),
            _synthetic_hit(
                f"distractor-{index}",
                project=project,
                content=f"{domain} unrelated historical note {index}",
                score=0.55,
                domain=domain,
                semantic_type="knowledge",
            ),
            _synthetic_hit(
                f"graph-{index}",
                project=project,
                content=f"{semantic_type} dependency context {index}",
                score=0.42,
                domain=domain,
                semantic_type="knowledge",
                graph_score=0.8,
            ),
            _synthetic_hit(
                f"cross-project-{index}",
                project="other-project",
                content=f"{query} cross-project candidate",
                score=0.99,
                domain=domain,
                semantic_type=semantic_type,
            ),
            _synthetic_hit(
                f"archived-{index}",
                project=project,
                content=f"{query} archived candidate",
                score=0.98,
                domain=domain,
                semantic_type=semantic_type,
                lifecycle="archived",
            ),
            _synthetic_hit(
                f"log-{index}",
                project=project,
                content=f"{query} raw log candidate",
                score=0.97,
                domain=domain,
                semantic_type="log",
            ),
        )
        cases.append(BenchmarkCase(query, project, frozenset({relevant_id}), hits))
    return cases


def evaluate_benchmark(
    cases: Sequence[BenchmarkCase],
    *,
    ranker: Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]],
    token_budget: int = 240,
    include_case_reports: bool = False,
) -> dict[str, Any]:
    if not cases:
        raise ValueError("benchmark requires at least one case")

    top1_hits = 0
    ndcg_total = 0.0
    filter_correct = 0
    context_budget_pass = 0
    leakage_count = 0
    case_reports: list[dict[str, Any]] = []

    for case in cases:
        filtered = filter_benchmark_hits(case.hits, project=case.project)
        filtered_ids = {_hit_id(hit) for hit in filtered}
        expected_ids = {
            _hit_id(hit)
            for hit in case.hits
            if _is_allowed_hit(hit, project=case.project)
        }
        filter_ok = filtered_ids == expected_ids
        filter_correct += int(filter_ok)
        leakage_count += sum(not _is_allowed_hit(hit, project=case.project) for hit in filtered)

        ranked = ranker(case.query, [dict(hit, metadata=dict(hit.get("metadata") or {})) for hit in filtered])
        ranked_ids = [_hit_id(hit) for hit in ranked[:5]]
        top1_ok = bool(ranked_ids and ranked_ids[0] in case.relevant_ids)
        top1_hits += int(top1_ok)
        ndcg = _ndcg_at_5(ranked_ids, case.relevant_ids)
        ndcg_total += ndcg

        context_items = [
            {
                "id": _hit_id(hit),
                "title": (hit.get("metadata") or {}).get("raw_title") or _hit_id(hit),
                "project": (hit.get("metadata") or {}).get("project"),
                "content": str(hit.get("content") or hit.get("memory") or ""),
                "score": float(hit.get("score") or 0.0),
                "context_origin": hit.get("context_origin") or "LOCAL",
            }
            for hit in ranked[:5]
        ]
        compiled = compile_context(context_items, token_budget=token_budget)
        budget_ok = compiled["estimated_tokens"] <= token_budget
        citations_ok = all(citation.get("project") == case.project for citation in compiled["citations"])
        context_budget_pass += int(budget_ok and citations_ok)
        case_reports.append(
            {
                "query": case.query,
                "top1": top1_ok,
                "ndcg_at_5": round(ndcg, 6),
                "filter_correct": filter_ok,
                "context_budget_ok": budget_ok and citations_ok,
                "ranked_ids": ranked_ids,
            }
        )

    total = len(cases)
    top1_accuracy = top1_hits / total
    ndcg_at_5 = ndcg_total / total
    filter_correctness = filter_correct / total
    context_budget_pass_rate = context_budget_pass / total
    report = {
        "cases": total,
        "top1_accuracy": round(top1_accuracy, 6),
        "ndcg_at_5": round(ndcg_at_5, 6),
        "filter_correctness": round(filter_correctness, 6),
        "context_budget_pass_rate": round(context_budget_pass_rate, 6),
        "leakage_count": leakage_count,
        "thresholds": {
            "top1_accuracy": 0.8,
            "ndcg_at_5": 0.8,
            "filter_correctness": 1.0,
            "context_budget_pass_rate": 1.0,
            "leakage_count": 0,
        },
        "ok": (
            top1_accuracy >= 0.8
            and ndcg_at_5 >= 0.8
            and filter_correctness == 1.0
            and context_budget_pass_rate == 1.0
            and leakage_count == 0
        ),
    }
    if include_case_reports:
        report["case_reports"] = case_reports
    return report


def filter_benchmark_hits(hits: Sequence[dict[str, Any]], *, project: str) -> list[dict[str, Any]]:
    return [hit for hit in hits if _is_allowed_hit(hit, project=project)]


def _is_allowed_hit(hit: dict[str, Any], *, project: str) -> bool:
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    lifecycle = str(metadata.get("lifecycle") or "").lower()
    semantic_type = str(metadata.get("semantic_type") or "").lower()
    return (
        metadata.get("project") == project
        and not metadata.get("archived_at")
        and lifecycle not in {"archived", "deprecated"}
        and semantic_type not in {"log", "error"}
    )


def _hit_id(hit: dict[str, Any]) -> str:
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    return str(metadata.get("source_id") or hit.get("source_id") or hit.get("id") or "")


def _ndcg_at_5(ranked_ids: Sequence[str], relevant_ids: frozenset[str]) -> float:
    gains = [3 if candidate in relevant_ids else 0 for candidate in ranked_ids[:5]]
    dcg = sum((2**gain - 1) / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = sorted(gains, reverse=True)
    ideal_dcg = sum((2**gain - 1) / math.log2(index + 2) for index, gain in enumerate(ideal))
    return dcg / ideal_dcg if ideal_dcg else 1.0


def _synthetic_hit(
    source_id: str,
    *,
    project: str,
    content: str,
    score: float,
    domain: str,
    semantic_type: str,
    lifecycle: str = "validated",
    graph_score: float | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source_id": source_id,
        "project": project,
        "raw_title": source_id,
        "domain": domain,
        "semantic_type": semantic_type,
        "lifecycle": lifecycle,
        "files": [],
        "tags": [domain, semantic_type],
    }
    if graph_score is not None:
        metadata["graph_score"] = graph_score
    return {
        "id": f"point-{source_id}",
        "content": content,
        "score": score,
        "context_origin": "LOCAL",
        "metadata": metadata,
    }


__all__ = [
    "BenchmarkCase",
    "DEFAULT_BENCHMARK_CASES",
    "MAX_BENCHMARK_CASES",
    "MIN_BENCHMARK_CASES",
    "build_default_benchmark_cases",
    "evaluate_benchmark",
    "filter_benchmark_hits",
]
