"""Deterministic, read-only BHM value benchmark.

The benchmark measures the effect of retrieval and bounded context assembly on
an agent-task proxy.  It intentionally does not call a model, network, SQLite,
Qdrant, or Mem0.  The result is therefore local replay evidence, not real-user
telemetry or a claim about model quality.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context_compiler import compile_context
from .retrieval_benchmark import filter_benchmark_hits
from .retrieval_fusion import weighted_rank_fusion


VALUE_BENCHMARK_SCHEMA_VERSION = "bhm.value-benchmark.v1"
DEFAULT_CASE_COUNT = 1000
DEFAULT_REPEAT_COUNT = 10
MIN_CASE_COUNT = 100
MAX_CASE_COUNT = 1000
MIN_REPEAT_COUNT = 1
MAX_REPEAT_COUNT = 100

MODES = ("no-memory", "file-only", "naive-vector", "bhm-no-graph", "bhm-no-filters", "bhm-full")
TASK_TYPES = ("memory-continuity", "code-navigation", "incident-recovery", "cross-agent")
DOMAINS = ("storage", "retrieval", "graph", "runtime", "security", "provenance", "tasks", "release")
VARIANTS = ("direct", "paraphrase", "graph", "scope", "stale", "conflict", "handoff", "tie")


@dataclass(frozen=True)
class ValueBenchmarkCase:
    case_id: str
    task_type: str
    variant: str
    query: str
    project: str
    target_id: str
    hits: tuple[dict[str, Any], ...]


def build_value_benchmark_cases(count: int = DEFAULT_CASE_COUNT) -> list[ValueBenchmarkCase]:
    """Build a bounded fixture with relevant items and hard negatives."""

    target = int(count)
    if not MIN_CASE_COUNT <= target <= MAX_CASE_COUNT:
        raise ValueError(f"count must be between {MIN_CASE_COUNT} and {MAX_CASE_COUNT}")

    cases: list[ValueBenchmarkCase] = []
    for index in range(target):
        task_type = TASK_TYPES[index % len(TASK_TYPES)]
        variant = VARIANTS[index % len(VARIANTS)]
        project = "blackholememory" if index % 2 == 0 else "e-github-workspace"
        domain = DOMAINS[index % len(DOMAINS)]
        query = f"{domain} {task_type} {variant} canonical decision tests"
        target_id = f"target-{index:04d}"
        target_content, target_score, target_graph = _target_profile(query, domain, task_type, variant)
        graph_content, graph_score, graph_signal = _graph_profile(domain, task_type, variant)
        cases.append(
            ValueBenchmarkCase(
                case_id=f"case-{index:04d}",
                task_type=task_type,
                variant=variant,
                query=query,
                project=project,
                target_id=target_id,
                hits=(
                    _hit(
                        target_id,
                        project=project,
                        content=target_content,
                        score=target_score,
                        semantic_type="decision",
                        graph_score=target_graph,
                        files=[f"docs/{domain}-decision.md", f"tests/test_{domain}.py"],
                    ),
                    _hit(
                        f"same-project-distractor-{index:04d}",
                        project=project,
                        content=f"{domain} unrelated historical implementation note",
                        score=0.61,
                        semantic_type="knowledge",
                        graph_score=0.10,
                        files=[f"docs/{domain}-history.md"],
                    ),
                    _hit(
                        f"cross-project-{index:04d}",
                        project="other-project",
                        content=f"{query}: unrelated project copy",
                        score=0.99,
                        semantic_type="decision",
                        graph_score=0.02,
                        files=["docs/other-project.md"],
                    ),
                    _hit(
                        f"archived-{index:04d}",
                        project=project,
                        content=f"{query}: obsolete archived decision",
                        score=0.98,
                        semantic_type="decision",
                        lifecycle="archived",
                        graph_score=0.01,
                        files=[f"docs/archive/{domain}.md"],
                    ),
                    _hit(
                        f"log-{index:04d}",
                        project=project,
                        content=f"{query}: raw incident log candidate",
                        score=0.97,
                        semantic_type="log",
                        graph_score=0.01,
                        files=[f"runtime/{domain}.log"],
                    ),
                    _hit(
                        f"graph-neighbor-{index:04d}",
                        project=project,
                        content=graph_content,
                        score=graph_score,
                        semantic_type="knowledge",
                        graph_score=graph_signal,
                        files=[f"src/{domain}/dependency.py"],
                    ),
                ),
            )
        )
    return cases


def run_value_benchmark(
    *,
    cases: Sequence[ValueBenchmarkCase] | None = None,
    repeats: int = DEFAULT_REPEAT_COUNT,
    case_count: int = DEFAULT_CASE_COUNT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run every mode repeatedly and return a receipt-ready report."""

    repeat_count = int(repeats)
    if not MIN_REPEAT_COUNT <= repeat_count <= MAX_REPEAT_COUNT:
        raise ValueError(f"repeats must be between {MIN_REPEAT_COUNT} and {MAX_REPEAT_COUNT}")
    fixture = list(cases) if cases is not None else build_value_benchmark_cases(case_count)
    if not fixture:
        raise ValueError("benchmark requires at least one case")

    fixture_digest = _sha256([_case_dict(case) for case in fixture])
    runs: list[dict[str, Any]] = []
    for repetition in range(1, repeat_count + 1):
        run_started = time.perf_counter()
        mode_reports: dict[str, dict[str, Any]] = {}
        for mode in MODES:
            mode_started = time.perf_counter()
            mode_reports[mode] = _evaluate_mode(fixture, mode)
            mode_reports[mode]["runner_wall_ms"] = round((time.perf_counter() - mode_started) * 1000.0, 3)
        runs.append(
            {
                "repetition": repetition,
                "modes": mode_reports,
                "runner_wall_ms": round((time.perf_counter() - run_started) * 1000.0, 3),
            }
        )

    aggregates = {mode: _aggregate_mode(runs, mode) for mode in MODES}
    stable_aggregates = {
        mode: {key: value for key, value in aggregate.items() if not key.startswith("runner_wall_ms")}
        for mode, aggregate in aggregates.items()
    }
    core = {
        "schema_version": VALUE_BENCHMARK_SCHEMA_VERSION,
        "benchmark": "BHM Value Benchmark v1",
        "case_count": len(fixture),
        "repeat_count": repeat_count,
        "fixture_digest": fixture_digest,
        "modes": list(MODES),
        "aggregates": stable_aggregates,
        "execution": {
            "model_called": False,
            "agent_started": False,
            "network_called": False,
            "sqlite_written": False,
            "qdrant_written": False,
            "mem0_written": False,
            "live_runtime_used": False,
        },
        "evidence_class": "deterministic-local-replay",
        "limitations": [
            "task_success_rate is a deterministic agent-task proxy, not model telemetry",
            "no live BHM runtime or external memory backend was used",
            "results measure the frozen fixture and do not establish universal model quality",
        ],
    }
    report = {
        **core,
        "report_digest": _sha256(core),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "generated_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "runs": runs,
    }
    report["aggregates"] = aggregates
    return report


def render_value_benchmark_markdown(report: Mapping[str, Any]) -> str:
    """Render a compact README-friendly summary from a report."""

    aggregates = report.get("aggregates") or {}
    lines = [
        "## BHM Value Benchmark",
        "",
        f"Deterministic local replay: {report.get('case_count')} cases × {report.get('repeat_count')} repetitions. "
        f"Evidence class: `{report.get('evidence_class')}`. No model, network, or live memory backend was used.",
        "",
        "| Mode | Task success | Recall@5 | Citation validity | Leakage | Context tokens | Runner p95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode in MODES:
        row = aggregates[mode]
        lines.append(
            f"| `{mode}` | {_pct(row['task_success_rate'])} | {_pct(row['recall_at_5'])} | "
            f"{_pct(row['citation_validity'])} | {row['leakage_count']} | {row['context_tokens_mean']:.1f} | "
            f"{row['runner_wall_ms_p95']:.3f} |"
        )
    lines.extend(
        [
            "",
            f"Fixture digest: `{report.get('fixture_digest')}`  ",
            f"Report digest: `{report.get('report_digest')}`",
            "",
            "> This benchmark measures retrieval/context impact on a deterministic task proxy. It is not real-user telemetry and does not claim universal model quality.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_value_benchmark_context(
    case: ValueBenchmarkCase,
    mode: str,
    *,
    token_budget: int = 240,
) -> dict[str, Any]:
    """Build the exact bounded context used by deterministic and model replay."""

    ranked = _rank_case(case, mode)
    context_items = [
        {
            "id": _hit_id(hit),
            "title": (hit.get("metadata") or {}).get("raw_title") or _hit_id(hit),
            "project": (hit.get("metadata") or {}).get("project"),
            "content": str(hit.get("content") or ""),
            "score": float(hit.get("score") or 0.0),
            "context_origin": "LOCAL",
            "metadata": hit.get("metadata") or {},
        }
        for hit in ranked[:5]
    ]
    compiled = compile_context(context_items, token_budget=token_budget)
    forbidden_ids = [_hit_id(hit) for hit in ranked[:5] if not _is_allowed_hit(hit, case.project)]
    return {
        "mode": mode,
        "ranked_ids": [_hit_id(hit) for hit in ranked[:5]],
        "allowed_ids": [_hit_id(hit) for hit in ranked[:5] if _is_allowed_hit(hit, case.project)],
        "forbidden_ids": forbidden_ids,
        "compiled": compiled,
    }


def value_benchmark_fixture_digest(cases: Sequence[ValueBenchmarkCase]) -> str:
    """Return the stable digest shared by deterministic and model replay."""

    return _sha256([_case_dict(case) for case in cases])


def _evaluate_mode(cases: Sequence[ValueBenchmarkCase], mode: str) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"unknown benchmark mode: {mode}")
    totals = {
        "top1_hits": 0,
        "recall_at_5": 0.0,
        "ndcg_at_5": 0.0,
        "task_success": 0,
        "citation_validity": 0.0,
        "project_scope": 0,
        "context_budget": 0,
        "context_tokens": 0,
        "leakage_count": 0,
        "manual_corrections": 0,
    }
    for case in cases:
        ranked = _rank_case(case, mode)
        ranked_ids = [_hit_id(hit) for hit in ranked[:5]]
        target_hit = bool(ranked_ids and ranked_ids[0] == case.target_id)
        recall = float(case.target_id in ranked_ids)
        ndcg = _ndcg_at_5(ranked_ids, case.target_id)
        context_items = [
            {
                "id": _hit_id(hit),
                "title": (hit.get("metadata") or {}).get("raw_title") or _hit_id(hit),
                "project": (hit.get("metadata") or {}).get("project"),
                "content": str(hit.get("content") or ""),
                "score": float(hit.get("score") or 0.0),
                "context_origin": "LOCAL",
                "metadata": hit.get("metadata") or {},
            }
            for hit in ranked[:5]
        ]
        compiled = compile_context(context_items, token_budget=240)
        citations = compiled.get("citations") or []
        citation_validity = _citation_validity(citations, case.project)
        leakage_count = sum(not _is_allowed_hit(hit, case.project) for hit in ranked[:5])
        project_scope = int(all(_is_allowed_hit(hit, case.project) for hit in ranked[:5]))
        budget_ok = int(int(compiled.get("estimated_tokens") or 0) <= 240)
        task_success = int(target_hit and leakage_count == 0 and citation_validity == 1.0 and budget_ok == 1)
        totals["top1_hits"] += int(target_hit)
        totals["recall_at_5"] += recall
        totals["ndcg_at_5"] += ndcg
        totals["task_success"] += task_success
        totals["citation_validity"] += citation_validity
        totals["project_scope"] += project_scope
        totals["context_budget"] += budget_ok
        totals["context_tokens"] += int(compiled.get("estimated_tokens") or 0)
        totals["leakage_count"] += leakage_count
        totals["manual_corrections"] += int(not task_success)

    count = len(cases)
    return {
        "cases": count,
        "top1_accuracy": round(totals["top1_hits"] / count, 6),
        "recall_at_5": round(totals["recall_at_5"] / count, 6),
        "ndcg_at_5": round(totals["ndcg_at_5"] / count, 6),
        "task_success_rate": round(totals["task_success"] / count, 6),
        "citation_validity": round(totals["citation_validity"] / count, 6),
        "project_scope_accuracy": round(totals["project_scope"] / count, 6),
        "context_budget_pass_rate": round(totals["context_budget"] / count, 6),
        "context_tokens_mean": round(totals["context_tokens"] / count, 3),
        "leakage_count": totals["leakage_count"],
        "manual_corrections": totals["manual_corrections"],
    }


def _rank_case(case: ValueBenchmarkCase, mode: str) -> list[dict[str, Any]]:
    if mode == "no-memory":
        return []
    if mode == "file-only":
        return sorted(case.hits, key=lambda hit: (-_lexical_score(case.query, hit), -float(hit.get("score") or 0.0), _hit_id(hit)))
    if mode == "naive-vector":
        return sorted(case.hits, key=lambda hit: (-float(hit.get("score") or 0.0), _hit_id(hit)))

    if mode not in {"bhm-no-graph", "bhm-no-filters", "bhm-full"}:
        raise ValueError(f"unknown benchmark mode: {mode}")
    allowed = list(case.hits) if mode == "bhm-no-filters" else filter_benchmark_hits(case.hits, project=case.project)
    semantic = _rank_channel(allowed, lambda hit: float(hit.get("score") or 0.0))
    lexical = _rank_channel(allowed, lambda hit: _lexical_score(case.query, hit))
    graph = (
        _rank_channel(allowed, lambda hit: float((hit.get("metadata") or {}).get("graph_score") or 0.0))
        if mode != "bhm-no-graph"
        else {}
    )
    fused = weighted_rank_fusion(
        {"semantic": semantic, "lexical": lexical, "graph": graph},
        weights={"semantic": 1.0, "lexical": 1.0, "graph": 0.7},
    )
    return sorted(allowed, key=lambda hit: (-float(fused.get(_hit_id(hit), 0.0)), _hit_id(hit)))


def _rank_channel(hits: Sequence[dict[str, Any]], score: Callable[[dict[str, Any]], float]) -> dict[str, int]:
    ordered = sorted(hits, key=lambda hit: (-float(score(hit)), _hit_id(hit)))
    return {_hit_id(hit): rank for rank, hit in enumerate(ordered, start=1)}


def _aggregate_mode(runs: Sequence[Mapping[str, Any]], mode: str) -> dict[str, Any]:
    metric_names = (
        "top1_accuracy",
        "recall_at_5",
        "ndcg_at_5",
        "task_success_rate",
        "citation_validity",
        "project_scope_accuracy",
        "context_budget_pass_rate",
        "context_tokens_mean",
        "leakage_count",
        "manual_corrections",
    )
    values = {name: [float(run["modes"][mode][name]) for run in runs] for name in metric_names}
    wall = [float(run["modes"][mode]["runner_wall_ms"]) for run in runs]
    result: dict[str, Any] = {"repetitions": len(runs)}
    for name, samples in values.items():
        result[name] = round(statistics.fmean(samples), 6)
        result[f"{name}_min"] = round(min(samples), 6)
        result[f"{name}_max"] = round(max(samples), 6)
    result["runner_wall_ms_mean"] = round(statistics.fmean(wall), 6)
    result["runner_wall_ms_p50"] = round(_percentile(wall, 0.50), 6)
    result["runner_wall_ms_p95"] = round(_percentile(wall, 0.95), 6)
    return result


def _hit(
    source_id: str,
    *,
    project: str,
    content: str,
    score: float,
    semantic_type: str,
    graph_score: float,
    files: list[str],
    lifecycle: str = "validated",
) -> dict[str, Any]:
    return {
        "id": f"point-{source_id}",
        "content": content,
        "score": score,
        "metadata": {
            "source_id": source_id,
            "raw_title": source_id,
            "project": project,
            "lifecycle": lifecycle,
            "semantic_type": semantic_type,
            "graph_score": graph_score,
            "source_refs": [f"benchmark/{source_id}"],
            "files": files,
        },
    }


def _target_profile(query: str, domain: str, task_type: str, variant: str) -> tuple[str, float, float]:
    if variant == "paraphrase":
        return (
            f"Validated rationale for {domain}: accepted implementation with regression coverage and canonical evidence.",
            0.93,
            0.82,
        )
    if variant == "graph":
        return (
            f"{domain} {task_type} dependency lineage, canonical decision and test evidence.",
            0.54,
            1.00,
        )
    if variant == "conflict":
        return (f"{query}: current validated decision supersedes the previous proposal.", 0.79, 0.90)
    if variant == "handoff":
        return (f"{query}: cross-agent handoff with session continuity and verified files.", 0.88, 0.91)
    if variant == "tie":
        return (f"{query}: canonical decision with verified implementation evidence.", 0.80, 0.86)
    return (f"{query}: validated implementation decision with test evidence.", 0.86, 0.96)


def _graph_profile(domain: str, task_type: str, variant: str) -> tuple[str, float, float]:
    if variant == "graph":
        return (f"{domain} {task_type} caller dependency context", 0.72, 0.05)
    return (f"{task_type} dependency and caller context", 0.44, 0.88)


def _lexical_score(query: str, hit: Mapping[str, Any]) -> float:
    query_tokens = _tokens(query)
    content_tokens = _tokens(hit.get("content"))
    return len(query_tokens & content_tokens) / max(len(query_tokens), 1)


def _tokens(value: Any) -> set[str]:
    return {token.casefold() for token in str(value or "").replace("-", " ").split() if len(token) > 1}


def _hit_id(hit: Mapping[str, Any]) -> str:
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
    return str(metadata.get("source_id") or hit.get("id") or "")


def _is_allowed_hit(hit: Mapping[str, Any], project: str) -> bool:
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
    lifecycle = str(metadata.get("lifecycle") or "").casefold()
    semantic_type = str(metadata.get("semantic_type") or "").casefold()
    return bool(
        metadata.get("project") == project
        and lifecycle not in {"archived", "deprecated"}
        and semantic_type not in {"log", "error"}
    )


def _citation_validity(citations: Sequence[Mapping[str, Any]], project: str) -> float:
    if not citations:
        return 0.0
    valid = 0
    for citation in citations:
        provenance = citation.get("provenance") if isinstance(citation.get("provenance"), Mapping) else {}
        valid += int(
            citation.get("project") == project
            and bool(provenance.get("source_id"))
            and bool(provenance.get("source_refs") or provenance.get("files"))
        )
    return valid / len(citations)


def _ndcg_at_5(ranked_ids: Sequence[str], target_id: str) -> float:
    for index, candidate in enumerate(ranked_ids[:5], start=1):
        if candidate == target_id:
            return round(1.0 / math.log2(index + 1), 6)
    return 0.0


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _case_dict(case: ValueBenchmarkCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "task_type": case.task_type,
        "variant": case.variant,
        "query": case.query,
        "project": case.project,
        "target_id": case.target_id,
        "hits": list(case.hits),
    }


def _sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _pct(value: float) -> str:
    return f"{float(value) * 100:.1f}%"


def write_value_benchmark_report(report: Mapping[str, Any], output_json: Path, output_markdown: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_markdown.write_text(render_value_benchmark_markdown(report), encoding="utf-8")


__all__ = [
    "DEFAULT_CASE_COUNT",
    "DEFAULT_REPEAT_COUNT",
    "MODES",
    "VALUE_BENCHMARK_SCHEMA_VERSION",
    "ValueBenchmarkCase",
    "build_value_benchmark_context",
    "build_value_benchmark_cases",
    "render_value_benchmark_markdown",
    "run_value_benchmark",
    "value_benchmark_fixture_digest",
    "write_value_benchmark_report",
]
