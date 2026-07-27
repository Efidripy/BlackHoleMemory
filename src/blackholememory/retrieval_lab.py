"""Bounded retrieval experiments for the local-LLM proposal contour.

The lab makes retrieval improvements measurable without replacing the
authoritative search path.  It produces deterministic query variants,
HyDE-style hypotheses, rerank evidence, hard negatives and benchmark/failure
cases; it never writes memories or changes ranking in production.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .llm_safety import sanitize_llm_value


RETRIEVAL_LAB_SCHEMA_VERSION = "bhm.llm.retrieval-lab.v1"
RETRIEVAL_LAB_MAX_CANDIDATES = 128
RETRIEVAL_LAB_MAX_QUERIES = 8
RETRIEVAL_LAB_MAX_BENCHMARK_CASES = 32
RETRIEVAL_LAB_MAX_FAILURE_CASES = 24
RETRIEVAL_LAB_MAX_TEXT = 900
RETRIEVAL_LAB_FEATURES = (
    "query_rewrite",
    "multi_query",
    "hyde",
    "rerank",
    "hard_negatives",
    "synthetic_benchmark",
    "failure_cases",
)


class RetrievalLabError(ValueError):
    """Raised when a retrieval experiment exceeds its deterministic bounds."""


def build_retrieval_lab_preview(
    query: str,
    *,
    project: str = "blackholememory",
    candidates: Sequence[Mapping[str, Any]] = (),
    feature_flags: Mapping[str, Any] | None = None,
    limit: int = 10,
    benchmark_cases: int = 8,
    latency_budget_ms: float = 250.0,
    observed_latency_ms: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a read-only retrieval experiment report."""

    if not 1 <= int(limit) <= 50:
        raise RetrievalLabError("limit must be between 1 and 50")
    if not 1 <= int(benchmark_cases) <= RETRIEVAL_LAB_MAX_BENCHMARK_CASES:
        raise RetrievalLabError(f"benchmark_cases must be between 1 and {RETRIEVAL_LAB_MAX_BENCHMARK_CASES}")
    if len(candidates) > RETRIEVAL_LAB_MAX_CANDIDATES:
        raise RetrievalLabError(f"candidates exceed limit {RETRIEVAL_LAB_MAX_CANDIDATES}")
    try:
        budget = float(latency_budget_ms)
    except (TypeError, ValueError):
        raise RetrievalLabError("latency_budget_ms must be numeric") from None
    if budget <= 0 or budget != budget:
        raise RetrievalLabError("latency_budget_ms must be positive and finite")
    if observed_latency_ms is not None:
        try:
            observed = float(observed_latency_ms)
        except (TypeError, ValueError):
            raise RetrievalLabError("observed_latency_ms must be numeric") from None
        if observed < 0 or observed != observed:
            raise RetrievalLabError("observed_latency_ms must be non-negative and finite")
    else:
        observed = None

    safe_project = _safe_text(project, "blackholememory", 120) or "blackholememory"
    safe_query = _safe_text(query, safe_project, 480)
    if not safe_query:
        raise RetrievalLabError("query is required")
    flags = _normalize_flags(feature_flags)
    normalized = _normalize_candidates(candidates, safe_project)
    allowed = [item for item in normalized if item["allowed"]]
    leakage_count = len(normalized) - len(allowed)
    rewrites = _query_rewrites(safe_query, safe_project, flags)
    multi_queries = _bounded_unique(
        [safe_query, *rewrites] if flags["multi_query"] else [safe_query],
        RETRIEVAL_LAB_MAX_QUERIES,
    )
    hyde = _hyde_candidates(safe_query, safe_project, flags)
    reranked = _rerank(allowed, safe_query, int(limit), flags)
    hard_negatives = _hard_negatives(normalized, allowed, safe_query, flags)
    benchmark = _synthetic_benchmark(safe_query, safe_project, multi_queries, int(benchmark_cases), flags)
    latency_gate = _latency_gate(observed, budget)
    filter_gate = {
        "passed": leakage_count == 0,
        "leakage_count": leakage_count,
        "scope": safe_project,
        "archived_and_log_filtered": True,
    }
    failures = _failure_cases(
        normalized,
        allowed,
        reranked,
        leakage_count,
        latency_gate,
        flags,
    )
    summary = {
        "candidate_count": len(normalized),
        "eligible_count": len(allowed),
        "reranked_count": len(reranked),
        "query_count": len(multi_queries),
        "hyde_count": len(hyde),
        "hard_negative_count": len(hard_negatives),
        "benchmark_count": len(benchmark),
        "failure_case_count": len(failures),
    }
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    core = {
        "query": safe_query,
        "project": safe_project,
        "feature_flags": flags,
        "summary": summary,
        "query_rewrites": rewrites,
        "multi_queries": multi_queries,
        "hyde_candidates": hyde,
        "reranked": reranked,
        "hard_negatives": hard_negatives,
        "synthetic_benchmark": benchmark,
        "failure_cases": failures,
        "filter_gate": filter_gate,
        "latency_gate": latency_gate,
        "generated_at": clock.isoformat().replace("+00:00", "Z"),
    }
    digest = _sha256(_canonical_json(core))
    return {
        "schema_version": RETRIEVAL_LAB_SCHEMA_VERSION,
        "preview_digest": digest,
        **core,
        "execution": {
            "model_started": False,
            "retrieval_path_mutated": False,
            "writes_performed": False,
            "auto_apply": False,
            "authority": "proposal",
        },
        "gates": {
            "filter": filter_gate,
            "latency": latency_gate,
            "leakage": {"passed": leakage_count == 0, "count": leakage_count},
            "benchmark_requires_labels": bool(benchmark),
            "failure_cases_require_review": bool(failures),
        },
    }


def verify_retrieval_lab_digest(preview: Mapping[str, Any]) -> bool:
    """Verify the digest of a retrieval lab preview."""

    expected = str(preview.get("preview_digest") or "")
    if not expected:
        return False
    core = {
        key: preview.get(key)
        for key in (
            "query",
            "project",
            "feature_flags",
            "summary",
            "query_rewrites",
            "multi_queries",
            "hyde_candidates",
            "reranked",
            "hard_negatives",
            "synthetic_benchmark",
            "failure_cases",
            "filter_gate",
            "latency_gate",
            "generated_at",
        )
    }
    return expected == _sha256(_canonical_json(core))


def _normalize_flags(raw: Mapping[str, Any] | None) -> dict[str, bool]:
    provided = dict(raw or {})
    unknown = sorted(set(provided) - set(RETRIEVAL_LAB_FEATURES))
    if unknown:
        raise RetrievalLabError(f"unsupported feature flags: {', '.join(unknown)}")
    return {name: bool(provided.get(name, True)) for name in RETRIEVAL_LAB_FEATURES}


def _normalize_candidates(candidates: Sequence[Mapping[str, Any]], project: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in candidates:
        item = dict(raw)
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        candidate_id = _safe_text(
            metadata.get("source_id") or item.get("source_id") or item.get("id"),
            project,
            180,
        )
        if not candidate_id:
            continue
        candidate_project = _safe_text(metadata.get("project") or item.get("project"), project, 120)
        lifecycle = _safe_text(metadata.get("lifecycle") or item.get("lifecycle"), project, 40).casefold()
        semantic_type = _safe_text(metadata.get("semantic_type") or item.get("semantic_type"), project, 80).casefold()
        archived = bool(metadata.get("archived_at") or item.get("archived_at") or lifecycle in {"archived", "deprecated"})
        content = _safe_text(
            metadata.get("raw_title") or item.get("content") or item.get("memory") or item.get("title"),
            project,
            RETRIEVAL_LAB_MAX_TEXT,
        )
        try:
            base_score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            base_score = 0.0
        base_score = round(min(max(base_score, 0.0), 1.0), 6)
        same_project = candidate_project.casefold() == project.casefold()
        allowed = (
            same_project
            and not archived
            and semantic_type not in {"log", "error"}
        )
        reasons: list[str] = []
        if not same_project:
            reasons.append("project_scope")
        if archived:
            reasons.append("archived_or_deprecated")
        if semantic_type in {"log", "error"}:
            reasons.append("log_or_error")
        normalized.append(
            {
                "candidate_id": candidate_id,
                "project": candidate_project,
                "content_excerpt": content,
                "base_score": base_score,
                "semantic_type": semantic_type or "knowledge",
                "allowed": allowed,
                "filter_reasons": reasons,
            }
        )
    return sorted(normalized, key=lambda item: (-float(item["base_score"]), item["candidate_id"]))


def _query_rewrites(query: str, project: str, flags: Mapping[str, bool]) -> list[str]:
    if not flags["query_rewrite"]:
        return []
    variants = (
        f"{query} implementation evidence",
        f"{query} validated decision constraints",
        f"{query} tests failure cases",
    )
    return _bounded_unique(_safe_text(item, project, 480) for item in variants)


def _hyde_candidates(query: str, project: str, flags: Mapping[str, bool]) -> list[dict[str, Any]]:
    if not flags["hyde"]:
        return []
    hypotheses = (
        f"A validated answer to '{query}' should cite project-scoped implementation evidence and tests.",
        f"A robust result for '{query}' should state constraints, failure modes and accepted evidence.",
    )
    result: list[dict[str, Any]] = []
    for ordinal, hypothesis in enumerate(hypotheses):
        safe_hypothesis = _safe_text(hypothesis, project, 420)
        result.append(
            {
                "candidate_id": f"hyde_{_sha256(f'{project}:{query}:{ordinal}')[:20]}",
                "hypothesis": safe_hypothesis,
                "query_digest": _sha256(query),
                "source": "deterministic-template",
                "authority": "proposal",
                "auto_apply": False,
            }
        )
    return result


def _rerank(
    candidates: Sequence[Mapping[str, Any]],
    query: str,
    limit: int,
    flags: Mapping[str, bool],
) -> list[dict[str, Any]]:
    query_tokens = _tokens(query)
    ranked: list[dict[str, Any]] = []
    for item in candidates:
        overlap = len(query_tokens & _tokens(item.get("content_excerpt"))) / max(len(query_tokens), 1)
        base_score = float(item.get("base_score") or 0.0)
        score = base_score if not flags["rerank"] else (0.65 * base_score) + (0.35 * overlap)
        reasons = ["project_filtered"]
        if overlap:
            reasons.append("lexical_overlap")
        if flags["rerank"]:
            reasons.append("deterministic_rerank")
        ranked.append(
            {
                "candidate_id": str(item["candidate_id"]),
                "score": round(min(max(score, 0.0), 1.0), 6),
                "overlap": round(overlap, 6),
                "reason_codes": reasons,
                "authority": "proposal",
            }
        )
    ranked.sort(key=lambda item: (-float(item["score"]), item["candidate_id"]))
    for rank, item in enumerate(ranked[:limit], start=1):
        item["rank"] = rank
    return ranked[:limit]


def _hard_negatives(
    normalized: Sequence[Mapping[str, Any]],
    allowed: Sequence[Mapping[str, Any]],
    query: str,
    flags: Mapping[str, bool],
) -> list[dict[str, Any]]:
    if not flags["hard_negatives"]:
        return []
    allowed_ids = {str(item["candidate_id"]) for item in allowed}
    query_tokens = _tokens(query)
    negatives: list[dict[str, Any]] = []
    for item in normalized:
        candidate_id = str(item["candidate_id"])
        if candidate_id not in allowed_ids:
            reason = list(item.get("filter_reasons") or []) or ["filtered_candidate"]
            negatives.append({"candidate_id": candidate_id, "reason_codes": reason, "source": "filter"})
            continue
        overlap = len(query_tokens & _tokens(item.get("content_excerpt"))) / max(len(query_tokens), 1)
        if overlap == 0 or float(item.get("base_score") or 0.0) < 0.4:
            negatives.append({"candidate_id": candidate_id, "reason_codes": ["low_relevance_signal"], "source": "ranker"})
    return negatives[:RETRIEVAL_LAB_MAX_FAILURE_CASES]


def _synthetic_benchmark(
    query: str,
    project: str,
    multi_queries: Sequence[str],
    count: int,
    flags: Mapping[str, bool],
) -> list[dict[str, Any]]:
    if not flags["synthetic_benchmark"]:
        return []
    result: list[dict[str, Any]] = []
    for index in range(min(count, RETRIEVAL_LAB_MAX_BENCHMARK_CASES)):
        variant = multi_queries[index % max(len(multi_queries), 1)]
        result.append(
            {
                "case_id": f"retrieval_case_{_sha256(f'{project}:{query}:{index}')[:20]}",
                "query": variant,
                "project": project,
                "label_required": True,
                "expected_top_ids": [],
                "leakage_gate": True,
                "evaluation_only": True,
            }
        )
    return result


def _failure_cases(
    normalized: Sequence[Mapping[str, Any]],
    allowed: Sequence[Mapping[str, Any]],
    reranked: Sequence[Mapping[str, Any]],
    leakage_count: int,
    latency_gate: Mapping[str, Any],
    flags: Mapping[str, bool],
) -> list[dict[str, Any]]:
    if not flags["failure_cases"]:
        return []
    failures: list[dict[str, Any]] = []
    if not normalized:
        failures.append({"code": "no_candidates", "severity": "warning", "requires_review": True})
    if not allowed and normalized:
        failures.append({"code": "all_candidates_filtered", "severity": "error", "requires_review": True})
    if leakage_count:
        failures.append({"code": "project_or_lifecycle_leakage", "severity": "error", "count": leakage_count, "requires_review": True})
    if allowed and not reranked:
        failures.append({"code": "rerank_empty", "severity": "warning", "requires_review": True})
    if latency_gate.get("status") == "breached":
        failures.append({"code": "latency_budget_breached", "severity": "error", "requires_review": True})
    elif latency_gate.get("status") == "not_measured":
        failures.append({"code": "latency_not_measured", "severity": "warning", "requires_review": True})
    return failures[:RETRIEVAL_LAB_MAX_FAILURE_CASES]


def _latency_gate(observed: float | None, budget: float) -> dict[str, Any]:
    if observed is None:
        return {"status": "not_measured", "passed": False, "observed_ms": None, "budget_ms": round(budget, 3)}
    passed = observed <= budget
    return {
        "status": "pass" if passed else "breached",
        "passed": passed,
        "observed_ms": round(observed, 3),
        "budget_ms": round(budget, 3),
    }


def _tokens(value: Any) -> set[str]:
    return {token for token in re.findall(r"[\w-]+", str(value or "").casefold()) if len(token) > 1}


def _bounded_unique(values: Sequence[str] | Any, limit: int = RETRIEVAL_LAB_MAX_QUERIES) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _safe_text(value: Any, project: str, limit: int) -> str:
    try:
        transformed = sanitize_llm_value(
            str(value or ""),
            source="retrieval-lab",
            project=project,
            max_input_bytes=16_384,
            max_sanitized_bytes=16_384,
        )
        return str(transformed.value or "").strip()[:limit]
    except ValueError:
        return ""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "RETRIEVAL_LAB_FEATURES",
    "RETRIEVAL_LAB_MAX_BENCHMARK_CASES",
    "RETRIEVAL_LAB_MAX_CANDIDATES",
    "RETRIEVAL_LAB_SCHEMA_VERSION",
    "RetrievalLabError",
    "build_retrieval_lab_preview",
    "verify_retrieval_lab_digest",
]
