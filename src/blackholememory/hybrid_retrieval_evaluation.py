"""Deterministic, in-memory evaluation for a proposed lexical retrieval lane.

WL-295.3 evaluates a possible future SQLite FTS5/BM25 candidate lane without
opening the authoritative SQLite store, Qdrant, Mem0, a model, or the network.
It deliberately does not alter the production retrieval path.  The fixture
models the realistic boundary where a semantic provider supplies a bounded
candidate set while a separate, project-filtered lexical candidate source can
recover exact identifiers that were not returned by that semantic set.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .retrieval_benchmark import filter_benchmark_hits
from .retrieval_fusion import weighted_rank_fusion


HYBRID_RETRIEVAL_EVALUATION_SCHEMA_VERSION = "bhm.hybrid-retrieval-evaluation.v1"
MIN_CASE_COUNT = 100
MAX_CASE_COUNT = 200
DEFAULT_CASE_COUNT = 120
DEFAULT_REPEATS = 11
MIN_REPEATS = 3
MAX_REPEATS = 31
TOP_K = 5
RRF_K = 60
RRF_WEIGHTS = {"current_bhm": 1.0, "sqlite_fts5_bm25": 1.0}
MAX_ACCEPTABLE_P95_REGRESSION = 0.20
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
_FTS_STOPWORDS = frozenset({"find", "for", "the", "a", "an", "to", "with"})
_IDENTIFIER_MIN_LENGTH = 8


@dataclass(frozen=True)
class HybridRetrievalCase:
    """One frozen retrieval case and the semantic lane presented to BHM today."""

    case_id: str
    scenario: str
    query: str
    project: str
    relevant_ids: frozenset[str]
    hits: tuple[dict[str, Any], ...]
    semantic_candidate_ids: tuple[str, ...]


def build_hybrid_retrieval_cases(count: int = DEFAULT_CASE_COUNT) -> list[HybridRetrievalCase]:
    """Build a bounded fixture covering identifier, semantic and isolation cases."""

    target_count = int(count)
    if not MIN_CASE_COUNT <= target_count <= MAX_CASE_COUNT:
        raise ValueError(f"count must be between {MIN_CASE_COUNT} and {MAX_CASE_COUNT}")

    scenarios = (
        "identifier_recovery",
        "paraphrase_semantic",
        "multi_term_lexical",
        "graph_corroboration",
        "stale_exclusion",
        "project_isolation",
    )
    cases: list[HybridRetrievalCase] = []
    for index in range(target_count):
        scenario = scenarios[index % len(scenarios)]
        project = "blackholememory" if index % 2 == 0 else "e-github-workspace"
        identifier = f"contract_{index:03d}_anchor"
        query = _query_for(scenario, identifier)
        target_id = f"case-{index:03d}-target"
        semantic_ids = (f"case-{index:03d}-semantic", f"case-{index:03d}-graph", f"case-{index:03d}-generic")
        target_score = 0.91 if scenario in {"paraphrase_semantic", "graph_corroboration"} else 0.21
        target_text = _target_text(scenario, query, identifier)
        hits = (
            _hit(target_id, project, target_text, target_score, "architecture", "validated", graph_score=0.86),
            _hit(semantic_ids[0], project, _semantic_text(scenario, index), 0.98, "knowledge", "validated", graph_score=0.08),
            _hit(semantic_ids[1], project, f"dependency evidence graph lineage {index}", 0.74, "knowledge", "validated", graph_score=0.94),
            _hit(semantic_ids[2], project, f"general implementation note for bounded retrieval {index}", 0.62, "knowledge", "validated", graph_score=0.04),
            _hit(f"case-{index:03d}-cross-project", "other-project", f"{query} private other project copy", 0.999, "architecture", "validated", graph_score=0.99),
            _hit(f"case-{index:03d}-archived", project, f"{query} obsolete superseded material", 0.997, "architecture", "archived", graph_score=0.99),
            _hit(f"case-{index:03d}-log", project, f"{query} raw incident log", 0.996, "log", "validated", graph_score=0.99),
        )
        # Identifier and multi-term targets deliberately model a semantic
        # candidate miss. All other cases retain the actual relevant target.
        supplied = semantic_ids if scenario in {"identifier_recovery", "multi_term_lexical", "project_isolation", "stale_exclusion"} else (target_id, *semantic_ids)
        cases.append(HybridRetrievalCase(f"hybrid-{index:03d}", scenario, query, project, frozenset({target_id}), hits, supplied))
    return cases


def build_sqlite_fts5_candidate_index(cases: Sequence[HybridRetrievalCase]) -> sqlite3.Connection:
    """Create an isolated FTS5 corpus containing all fixture rows, never live data."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE fixture_fts USING fts5(case_id UNINDEXED, source_id UNINDEXED, project UNINDEXED, lifecycle UNINDEXED, semantic_type UNINDEXED, content)"
        )
    except sqlite3.OperationalError as exc:
        connection.close()
        raise RuntimeError("SQLite FTS5 is required for WL-295.3 evaluation") from exc
    rows: list[tuple[str, str, str, str, str, str]] = []
    for case in cases:
        for hit in case.hits:
            metadata = _metadata(hit)
            rows.append((case.case_id, _hit_id(hit), str(metadata.get("project") or ""), str(metadata.get("lifecycle") or ""), str(metadata.get("semantic_type") or ""), str(hit.get("content") or "")))
    connection.executemany("INSERT INTO fixture_fts(case_id,source_id,project,lifecycle,semantic_type,content) VALUES(?,?,?,?,?,?)", rows)
    return connection


def build_exact_identifier_candidate_index(cases: Sequence[HybridRetrievalCase]) -> sqlite3.Connection:
    """Build a bounded, project-scoped exact-identifier index from active fixture rows.

    This models a future inexpensive exact-key route (for example, a B-tree
    backed identifier table) rather than disguising a cached FTS5 result as a
    latency improvement.  It deliberately indexes only high-signal tokens and
    applies project/lifecycle/type filtering before a candidate can be read.
    """

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE fixture_exact_identifier("
        "project TEXT NOT NULL, token TEXT NOT NULL, source_id TEXT NOT NULL, "
        "PRIMARY KEY(project, token, source_id)) WITHOUT ROWID"
    )
    rows: set[tuple[str, str, str]] = set()
    for case in cases:
        for hit in case.hits:
            metadata = _metadata(hit)
            project = str(metadata.get("project") or "")
            lifecycle = str(metadata.get("lifecycle") or "").casefold()
            semantic_type = str(metadata.get("semantic_type") or "").casefold()
            source_id = _hit_id(hit)
            if not project or not source_id or lifecycle in {"archived", "deprecated"} or semantic_type in {"log", "error"}:
                continue
            rows.update((project, token, source_id) for token in _exact_identifier_tokens(str(hit.get("content") or "")))
    connection.executemany(
        "INSERT INTO fixture_exact_identifier(project, token, source_id) VALUES(?, ?, ?)",
        sorted(rows),
    )
    return connection


def sqlite_fts5_bm25_rank(connection: sqlite3.Connection, case: HybridRetrievalCase, *, limit: int = TOP_K) -> list[str]:
    """Return project-scoped, active non-log BM25 candidate IDs for one fixture case."""

    bounded_limit = max(1, min(int(limit), TOP_K))
    match = _fts_match(case.query)
    if not match:
        return []
    rows = connection.execute(
        """
        SELECT source_id, bm25(fixture_fts) AS rank
        FROM fixture_fts
        WHERE fixture_fts MATCH ?
          AND case_id = ?
          AND project = ?
          AND lower(lifecycle) NOT IN ('archived', 'deprecated')
          AND lower(semantic_type) NOT IN ('log', 'error')
        ORDER BY rank, source_id
        LIMIT ?
        """,
        (match, case.case_id, case.project, bounded_limit),
    ).fetchall()
    return [str(row["source_id"]) for row in rows]


def exact_identifier_rank(
    connection: sqlite3.Connection,
    case: HybridRetrievalCase,
    *,
    limit: int = TOP_K,
) -> list[str]:
    """Return active, project-scoped candidates for unambiguous query identifiers."""

    bounded_limit = max(1, min(int(limit), TOP_K))
    result: list[str] = []
    for token in _exact_identifier_tokens(case.query):
        rows = connection.execute(
            "SELECT source_id FROM fixture_exact_identifier WHERE project = ? AND token = ? ORDER BY source_id LIMIT ?",
            (case.project, token, bounded_limit - len(result)),
        ).fetchall()
        for row in rows:
            normalized = str(row["source_id"])
            if normalized and normalized not in result:
                result.append(normalized)
            if len(result) >= bounded_limit:
                return result
    return result


def current_bhm_rank(case: HybridRetrievalCase) -> list[str]:
    """Invoke the unchanged current BHM ranker on its supplied semantic candidates."""

    # Delayed import keeps simple unit tests independent of FastAPI startup and
    # documents precisely which production function provides the baseline.
    from .app import _rank_hybrid_vector_hits

    candidates = { _hit_id(hit): hit for hit in filter_benchmark_hits(case.hits, project=case.project) }
    supplied = [dict(candidates[source_id], metadata=dict(_metadata(candidates[source_id]))) for source_id in case.semantic_candidate_ids if source_id in candidates]
    return [_hit_id(hit) for hit in _rank_hybrid_vector_hits(case.query, supplied)]


def candidate_augmented_rank(current_ids: Sequence[str], lexical_ids: Sequence[str]) -> list[str]:
    """Keep current ordering and append only new lexical candidates deterministically."""

    result: list[str] = []
    for source_id in (*current_ids, *lexical_ids):
        normalized = str(source_id)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def fixed_rrf_rank(current_ids: Sequence[str], lexical_ids: Sequence[str]) -> list[str]:
    """Fuse the two frozen ranks with fixed equal-weight reciprocal-rank fusion."""

    channels = {
        "current_bhm": {source_id: rank for rank, source_id in enumerate(current_ids, start=1)},
        "sqlite_fts5_bm25": {source_id: rank for rank, source_id in enumerate(lexical_ids, start=1)},
    }
    scores = weighted_rank_fusion(channels, k=RRF_K, weights=RRF_WEIGHTS)
    return [source_id for source_id, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def evaluate_hybrid_retrieval(
    *,
    cases: Sequence[HybridRetrievalCase] | None = None,
    case_count: int = DEFAULT_CASE_COUNT,
    repeats: int = DEFAULT_REPEATS,
    current_ranker: Callable[[HybridRetrievalCase], list[str]] = current_bhm_rank,
) -> dict[str, Any]:
    """Evaluate baseline, lexical candidate expansion and fixed RRF on a frozen corpus."""

    repeat_count = int(repeats)
    if not MIN_REPEATS <= repeat_count <= MAX_REPEATS:
        raise ValueError(f"repeats must be between {MIN_REPEATS} and {MAX_REPEATS}")
    fixture = list(cases) if cases is not None else build_hybrid_retrieval_cases(case_count)
    if not fixture:
        raise ValueError("evaluation requires at least one case")
    _validate_fixture(fixture)
    fixture_digest = _fixture_digest(fixture)
    connection = build_sqlite_fts5_candidate_index(fixture)
    exact_identifier_index = build_exact_identifier_candidate_index(fixture)
    try:
        stable_runs: list[dict[str, dict[str, Any]]] = []
        latency_samples: dict[str, list[float]] = {
            "current_bhm": [],
            "current_plus_fts5_candidate": [],
            "fixed_rrf": [],
            "current_plus_exact_identifier": [],
            "exact_identifier_fixed_rrf": [],
        }
        for _ in range(repeat_count):
            per_mode = {mode: _empty_totals() for mode in latency_samples}
            for case in fixture:
                current_started = time.perf_counter_ns()
                current_ids = current_ranker(case)
                current_latency_ms = _elapsed_ms(current_started)
                latency_samples["current_bhm"].append(current_latency_ms)
                lexical_started = time.perf_counter_ns()
                lexical_ids = sqlite_fts5_bm25_rank(connection, case)
                candidate_ids = candidate_augmented_rank(current_ids, lexical_ids)
                lexical_latency_ms = _elapsed_ms(lexical_started)
                exact_started = time.perf_counter_ns()
                exact_ids = exact_identifier_rank(exact_identifier_index, case)
                exact_candidate_ids = candidate_augmented_rank(current_ids, exact_ids)
                exact_latency_ms = _elapsed_ms(exact_started)
                # The candidate and RRF lanes are possible additions to the
                # current path, so their p95 budgets include the baseline
                # ranker rather than timing only their incremental operation.
                latency_samples["current_plus_fts5_candidate"].append(current_latency_ms + lexical_latency_ms)
                rrf_started = time.perf_counter_ns()
                rrf_ids = fixed_rrf_rank(current_ids, lexical_ids)
                latency_samples["fixed_rrf"].append(current_latency_ms + lexical_latency_ms + _elapsed_ms(rrf_started))
                exact_rrf_started = time.perf_counter_ns()
                exact_rrf_ids = fixed_rrf_rank(current_ids, exact_ids)
                latency_samples["current_plus_exact_identifier"].append(current_latency_ms + exact_latency_ms)
                latency_samples["exact_identifier_fixed_rrf"].append(current_latency_ms + exact_latency_ms + _elapsed_ms(exact_rrf_started))
                _record(per_mode["current_bhm"], case, current_ids)
                _record(per_mode["current_plus_fts5_candidate"], case, candidate_ids)
                _record(per_mode["fixed_rrf"], case, rrf_ids)
                _record(per_mode["current_plus_exact_identifier"], case, exact_candidate_ids)
                _record(per_mode["exact_identifier_fixed_rrf"], case, exact_rrf_ids)
            stable_runs.append({mode: _finalize_totals(totals, len(fixture)) for mode, totals in per_mode.items()})
    finally:
        connection.close()
        exact_identifier_index.close()

    modes = {mode: _aggregate_mode(stable_runs, latency_samples[mode], mode) for mode in latency_samples}
    recommendation = promotion_recommendation(modes)
    core = {
        "schema_version": HYBRID_RETRIEVAL_EVALUATION_SCHEMA_VERSION,
        "benchmark": "WL-295.3 offline hybrid retrieval evaluation",
        "fixture_digest": fixture_digest,
        "case_count": len(fixture),
        "repeat_count": repeat_count,
        "modes": modes,
        "fixed_rrf": {"k": RRF_K, "weights": dict(RRF_WEIGHTS)},
        "exact_identifier_route": {
            "mode": "project-scoped-active-high-signal-identifiers-only",
            "fixture_indexed_in_memory": "sqlite-primary-key-without-rowid",
            "production_retrieval_changed": False,
        },
        "promotion_gate": recommendation,
        "execution": {
            "sqlite_mode": "in-memory-fts5-fixture-only",
            "authoritative_sqlite_opened": False,
            "sqlite_written": False,
            "qdrant_called": False,
            "mem0_called": False,
            "model_called": False,
            "network_called": False,
            "production_retrieval_changed": False,
        },
        "limitations": [
            "the fixture is deterministic local evidence, not production telemetry",
            "the current baseline uses the unchanged _rank_hybrid_vector_hits function on its bounded semantic candidate set",
            "a passing gate authorizes only a separately reviewed feature-flag proposal, never automatic promotion",
        ],
    }
    return {**core, "report_digest": _sha256(core)}


def promotion_recommendation(modes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Apply WL-295.3's explicit, strict promotion rule to aggregate metrics."""

    baseline = modes.get("current_bhm") or {}
    baseline_recall = float(baseline.get("recall_at_5") or 0.0)
    baseline_p95 = float(baseline.get("p95_latency_ms") or 0.0)
    candidate_specs = (
        ("exact_identifier", "current_plus_exact_identifier", "exact_identifier_fixed_rrf"),
        ("fts5", "current_plus_fts5_candidate", "fixed_rrf"),
    )
    candidate_results: dict[str, dict[str, Any]] = {}
    for name, candidate_mode, fused_mode in candidate_specs:
        candidate = modes.get(candidate_mode)
        fused = modes.get(fused_mode)
        if candidate is None or fused is None:
            continue
        fused_p95 = float(fused.get("p95_latency_ms") or 0.0)
        p95_regression = ((fused_p95 - baseline_p95) / baseline_p95) if baseline_p95 > 0 else float("inf")
        checks = {
            "measurable_recall_gain": float(candidate.get("recall_at_5") or 0.0) > baseline_recall and float(fused.get("recall_at_5") or 0.0) > baseline_recall,
            "zero_project_isolation_regression": int(candidate.get("project_leakage_count") or 0) == 0 and int(fused.get("project_leakage_count") or 0) == 0,
            "p95_within_20_percent": p95_regression <= MAX_ACCEPTABLE_P95_REGRESSION,
        }
        candidate_results[name] = {
            "candidate_mode": candidate_mode,
            "fused_mode": fused_mode,
            "eligible_for_feature_flag_proposal": all(checks.values()),
            "checks": checks,
            "candidate_recall_at_5": round(float(candidate.get("recall_at_5") or 0.0), 6),
            "fused_recall_at_5": round(float(fused.get("recall_at_5") or 0.0), 6),
            "fused_p95_regression_ratio": round(p95_regression, 6) if p95_regression != float("inf") else None,
        }
    selected_name = next((name for name, result in candidate_results.items() if result["eligible_for_feature_flag_proposal"]), None)
    selected = candidate_results.get(selected_name or "")
    fallback = candidate_results.get("fts5") or next(iter(candidate_results.values()), {})
    report = selected or fallback
    checks = dict(report.get("checks") or {})
    return {
        "eligible_for_feature_flag_proposal": bool(selected),
        "decision": "propose-feature-flag" if selected else "defer",
        "selected_candidate": selected_name,
        "checks": checks,
        "candidates": candidate_results,
        "baseline_recall_at_5": round(baseline_recall, 6),
        "candidate_recall_at_5": report.get("candidate_recall_at_5", 0.0),
        "rrf_recall_at_5": report.get("fused_recall_at_5", 0.0),
        "rrf_p95_regression_ratio": report.get("fused_p95_regression_ratio"),
        "max_p95_regression_ratio": MAX_ACCEPTABLE_P95_REGRESSION,
    }


def _record(totals: dict[str, float], case: HybridRetrievalCase, ranked_ids: Sequence[str]) -> None:
    top = list(ranked_ids[:TOP_K])
    totals["recall_at_5"] += float(bool(case.relevant_ids.intersection(top)))
    totals["mrr"] += _mrr(top, case.relevant_ids)
    totals["project_leakage_count"] += float(sum(_is_forbidden(case, source_id) for source_id in top))
    totals["scenario_cases"] += 1.0


def _empty_totals() -> dict[str, float]:
    return {"recall_at_5": 0.0, "mrr": 0.0, "project_leakage_count": 0.0, "scenario_cases": 0.0}


def _finalize_totals(totals: Mapping[str, float], count: int) -> dict[str, Any]:
    return {
        "recall_at_5": round(float(totals["recall_at_5"]) / count, 6),
        "mrr": round(float(totals["mrr"]) / count, 6),
        "project_leakage_count": int(totals["project_leakage_count"]),
        "cases": int(totals["scenario_cases"]),
    }


def _aggregate_mode(runs: Sequence[Mapping[str, Mapping[str, Any]]], latencies: Sequence[float], mode: str) -> dict[str, Any]:
    values = [run[mode] for run in runs]
    return {
        "recall_at_5": round(statistics.fmean(float(item["recall_at_5"]) for item in values), 6),
        "mrr": round(statistics.fmean(float(item["mrr"]) for item in values), 6),
        "project_leakage_count": int(max(int(item["project_leakage_count"]) for item in values)),
        "cases": int(values[0]["cases"]),
        "p50_latency_ms": round(_percentile(latencies, 0.50), 6),
        "p95_latency_ms": round(_percentile(latencies, 0.95), 6),
        "latency_samples": len(latencies),
    }


def _validate_fixture(cases: Sequence[HybridRetrievalCase]) -> None:
    for case in cases:
        ids = {_hit_id(hit) for hit in case.hits}
        if not case.relevant_ids.issubset(ids):
            raise ValueError(f"case {case.case_id} relevant IDs are absent from its fixture")
        if not set(case.semantic_candidate_ids).issubset(ids):
            raise ValueError(f"case {case.case_id} semantic candidate IDs are absent from its fixture")


def _hit(source_id: str, project: str, content: str, score: float, semantic_type: str, lifecycle: str, *, graph_score: float) -> dict[str, Any]:
    return {"id": f"point-{source_id}", "content": content, "score": score, "context_origin": "LOCAL", "metadata": {"source_id": source_id, "project": project, "semantic_type": semantic_type, "lifecycle": lifecycle, "graph_score": graph_score}}


def _query_for(scenario: str, identifier: str) -> str:
    if scenario == "paraphrase_semantic":
        return "restore the canonical storage recovery decision"
    if scenario == "graph_corroboration":
        return "trace dependency lineage for storage recovery"
    if scenario == "multi_term_lexical":
        return f"retry budget {identifier}"
    return f"find {identifier} retrieval contract"


def _target_text(scenario: str, query: str, identifier: str) -> str:
    if scenario == "paraphrase_semantic":
        return "validated decision for reopening durable SQLite authority after a service interruption"
    if scenario == "graph_corroboration":
        return "storage recovery dependency lineage validated with graph evidence"
    if scenario == "multi_term_lexical":
        return f"{identifier} documents retry budget enforcement and bounded deadline ownership"
    return f"{identifier} is the validated retrieval contract anchor with regression coverage"


def _semantic_text(scenario: str, index: int) -> str:
    if scenario == "paraphrase_semantic":
        return f"canonical storage recovery decision supported by semantic evidence {index}"
    if scenario == "graph_corroboration":
        return f"dependency lineage for storage recovery graph evidence {index}"
    return f"semantically adjacent historical retrieval note {index}"


def _metadata(hit: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = hit.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _hit_id(hit: Mapping[str, Any]) -> str:
    return str(_metadata(hit).get("source_id") or hit.get("id") or "")


def _is_forbidden(case: HybridRetrievalCase, source_id: str) -> bool:
    hit = next((item for item in case.hits if _hit_id(item) == source_id), None)
    if hit is None:
        return True
    metadata = _metadata(hit)
    return str(metadata.get("project") or "") != case.project or str(metadata.get("lifecycle") or "").casefold() in {"archived", "deprecated"} or str(metadata.get("semantic_type") or "").casefold() in {"log", "error"}


def _fts_match(query: str) -> str:
    tokens = [token for token in _TOKEN_RE.findall(str(query or "")) if token.casefold() not in _FTS_STOPWORDS]
    return " AND ".join(f'"{token.replace(chr(34), "")}"' for token in tokens[:16])


def _exact_identifier_tokens(value: str) -> tuple[str, ...]:
    """Return only high-signal exact identifiers; natural-language terms cannot route here."""

    return tuple(
        dict.fromkeys(
            token.casefold()
            for token in _TOKEN_RE.findall(str(value or ""))
            if len(token) >= _IDENTIFIER_MIN_LENGTH and "_" in token and any(char.isdigit() for char in token)
        )
    )


def _mrr(ranked_ids: Sequence[str], relevant_ids: frozenset[str]) -> float:
    for rank, source_id in enumerate(ranked_ids, start=1):
        if source_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def _fixture_digest(cases: Sequence[HybridRetrievalCase]) -> str:
    return _sha256([{"case_id": case.case_id, "scenario": case.scenario, "query": case.query, "project": case.project, "relevant_ids": sorted(case.relevant_ids), "hits": list(case.hits), "semantic_candidate_ids": list(case.semantic_candidate_ids)} for case in cases])


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _elapsed_ms(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1_000_000.0


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


__all__ = [
    "DEFAULT_CASE_COUNT",
    "DEFAULT_REPEATS",
    "HYBRID_RETRIEVAL_EVALUATION_SCHEMA_VERSION",
    "HybridRetrievalCase",
    "RRF_K",
    "RRF_WEIGHTS",
    "build_hybrid_retrieval_cases",
    "build_exact_identifier_candidate_index",
    "build_sqlite_fts5_candidate_index",
    "candidate_augmented_rank",
    "current_bhm_rank",
    "exact_identifier_rank",
    "evaluate_hybrid_retrieval",
    "fixed_rrf_rank",
    "promotion_recommendation",
    "sqlite_fts5_bm25_rank",
]
