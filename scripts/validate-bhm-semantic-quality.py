#!/usr/bin/env python
"""Bounded, read-only semantic retrieval quality/freshness/provider gate.

This WI-82 probe deliberately keeps the semantic channel observational.  It
checks the local runtime's authority/cutover/provider health, verifies that a
repository snapshot is fresh and parse-clean, and runs a small deterministic
rank-fusion benchmark.  The live semantic request is optional and reports
``disabled``/``unavailable`` honestly; the probe never enables a feature flag,
starts a model, writes SQLite/Qdrant, or returns source text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.request import Request


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from blackholememory.code_search import fuse_code_search_matches  # noqa: E402
from blackholememory.local_endpoint_policy import MAX_RESPONSE_BYTES  # noqa: E402
from blackholememory.local_endpoint_policy import open_local_url  # noqa: E402
from blackholememory.local_endpoint_policy import read_bounded_response  # noqa: E402
from bhm_runtime_endpoints import validate_loopback_endpoint  # noqa: E402


SCHEMA_VERSION = "bhm.p28.wi82.semantic-quality.v1"
MAX_BENCHMARK_CASES = 32
MAX_LIVE_QUERIES = 8
DEFAULT_QUERIES = ("workManager", "graph", "runtime")
DEFAULT_MAX_SNAPSHOT_AGE_SECONDS = 86_400.0


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _bounded_text(value: Any, limit: int = 240) -> str:
    return str(value or "").replace("\x00", "")[:limit]


def _safe_base_url(value: str) -> str:
    try:
        return validate_loopback_endpoint(value)
    except Exception as exc:
        raise ValueError("base URL must target the local BHM runtime") from exc


def _safe_error(exc: BaseException) -> str:
    # Do not echo request headers, URLs with query strings, or credentials.
    return _bounded_text(str(exc), 300)


def _semantic_case(index: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Return a lexical/semantic pair where rank fusion should recover target."""

    target = f"src/target_{index:02d}.py"
    distractor = f"src/distractor_{index:02d}.py"
    lexical = [
        {"path": distractor, "language": "python", "score": 0.91, "match_kind": "metadata"},
        {"path": target, "language": "python", "score": 0.90, "match_kind": "metadata"},
    ]
    semantic = [
        {"path": target, "score": 0.99, "metadata": {"source_id": target}},
        {"path": distractor, "score": 0.10, "metadata": {"source_id": distractor}},
    ]
    return lexical, semantic, target


def run_semantic_benchmark(cases: int = 16) -> dict[str, Any]:
    """Evaluate rank-fusion invariants without calling a provider."""

    count = max(1, min(int(cases), MAX_BENCHMARK_CASES))
    # ``fuse_code_search_matches`` is intentionally opt-in.  Enabling the
    # process-local flag for this synthetic fixture does not touch the live
    # runtime and never starts an embedding model/provider.
    previous = os.environ.get("BHM_CODE_SEMANTIC_FUSION")
    os.environ["BHM_CODE_SEMANTIC_FUSION"] = "1"
    rows: list[dict[str, Any]] = []
    try:
        for index in range(count):
            lexical, semantic, expected = _semantic_case(index)
            # Keep the semantic channel below the public 0.75 ceiling while
            # making the expected rank change unambiguous for this fixture.
            fused = fuse_code_search_matches(lexical, semantic, limit=2, semantic_weight=0.7)
            top_path = str(fused[0].get("path") or "") if fused else ""
            fused_rank = next((rank for rank, item in enumerate(fused, start=1) if str(item.get("path") or "") == expected), None)
            baseline_rank = next((rank for rank, item in enumerate(lexical, start=1) if str(item.get("path") or "") == expected), None)
            metadata_only = all("content" not in item and "snippet" not in item for item in fused)
            bounded_scores = all(0.0 <= float(item.get("fusion_score") or 0.0) <= 1.0 for item in fused)
            rows.append(
                {
                    "case_id": f"semantic_case_{index:02d}",
                    "expected_top_path": expected,
                    "observed_top_path": top_path,
                    "top1_correct": top_path == expected,
                    "baseline_rank": baseline_rank,
                    "fused_rank": fused_rank,
                    "rank_improved": bool(fused_rank and baseline_rank and fused_rank < baseline_rank),
                    "metadata_only": metadata_only,
                    "scores_bounded": bounded_scores,
                }
            )
    finally:
        if previous is None:
            os.environ.pop("BHM_CODE_SEMANTIC_FUSION", None)
        else:
            os.environ["BHM_CODE_SEMANTIC_FUSION"] = previous
    top1 = sum(bool(row["top1_correct"]) for row in rows)
    baseline_top1 = sum(bool(row["baseline_rank"] == 1) for row in rows)
    reciprocal_rank = sum(1.0 / max(int(row["fused_rank"] or count + 1), 1) for row in rows)
    ndcg_at_2 = sum(
        1.0 / math.log2(max(int(row["fused_rank"] or count + 1), 1) + 1)
        if int(row["fused_rank"] or count + 1) <= 2
        else 0.0
        for row in rows
    )
    rank_improvements = sum(bool(row["rank_improved"]) for row in rows)
    metadata_only = all(bool(row["metadata_only"]) for row in rows)
    scores_bounded = all(bool(row["scores_bounded"]) for row in rows)
    result = {
        "schema_version": "bhm.code-search.semantic-quality-benchmark.v1",
        "cases": count,
        "semantic_weight": 0.7,
        "semantic_weight_bounded": True,
        "top1_correct": top1,
        "top1_accuracy": round(top1 / max(count, 1), 6),
        "baseline_top1_correct": baseline_top1,
        "baseline_top1_accuracy": round(baseline_top1 / max(count, 1), 6),
        "mean_reciprocal_rank": round(reciprocal_rank / max(count, 1), 6),
        "ndcg_at_2": round(ndcg_at_2 / max(count, 1), 6),
        "rank_improvement_count": rank_improvements,
        "metadata_only": metadata_only,
        "scores_bounded": scores_bounded,
        "provider_calls": 0,
        "model_started": False,
        "writes_sqlite_state": False,
        "writes_qdrant": False,
        "raw_source_returned": False,
        "rows": rows,
    }
    result["digest"] = _sha256({key: value for key, value in result.items() if key != "digest"})
    result["ok"] = bool(top1 == count and metadata_only and scores_bounded and rank_improvements == count)
    return result


def _response_json(response: Any) -> dict[str, Any]:
    payload = json.loads(read_bounded_response(response, limit=MAX_RESPONSE_BYTES).decode("utf-8"))
    return payload if isinstance(payload, dict) else {"value": payload}


class _LocalRuntimeClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 20.0):
        self.base_url = _safe_base_url(base_url)
        self.token = str(token or "").strip()
        self.timeout = max(1.0, min(float(timeout), 60.0))

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None
        if payload is not None:
            body = _canonical_json(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, method=method.upper(), headers=headers)
        try:
            with open_local_url(request, timeout=self.timeout) as response:
                return _response_json(response)
        except (TimeoutError, OSError, ValueError) as exc:
            raise RuntimeError(f"{method.upper()} {path}: {_safe_error(exc)}") from exc

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, payload)


def _runtime_health(client: _LocalRuntimeClient) -> dict[str, Any]:
    try:
        health = client.get("/bhm/health")
        cutover = client.get("/health/cutover")
        slo = client.get("/bhm/health/slo")
    except RuntimeError as exc:
        return {"ok": False, "error": _safe_error(exc), "status": "unavailable"}
    memory = health.get("memory_store") if isinstance(health.get("memory_store"), Mapping) else {}
    mem0 = cutover.get("mem0") if isinstance(cutover.get("mem0"), Mapping) else {}
    observed = slo.get("observed") if isinstance(slo.get("observed"), Mapping) else {}
    checks = slo.get("checks") if isinstance(slo.get("checks"), Mapping) else {}
    provider_ready = bool(observed.get("provider_ready", health.get("provider_ready", False)))
    qdrant_healthy = bool(observed.get("qdrant_healthy", health.get("qdrant_healthy", False)))
    authority_ok = (
        memory.get("backend") == "sqlite-authoritative"
        and bool(memory.get("ready"))
        and bool(cutover.get("ok"))
        and mem0.get("status") == "projection-only"
        and not bool(mem0.get("direct_vector_writes"))
    )
    provider_ok = provider_ready and qdrant_healthy
    slo_ok = str(slo.get("status") or "").casefold() == "healthy"
    return {
        "ok": bool(str(health.get("status") or "").casefold() == "healthy" and authority_ok and provider_ok and slo_ok),
        "health_status": health.get("status"),
        "version": health.get("version"),
        "authority": {
            "ok": authority_ok,
            "memory_store": memory.get("backend"),
            "memory_ready": bool(memory.get("ready")),
            "cutover_ok": bool(cutover.get("ok")),
            "mem0_status": mem0.get("status"),
            "direct_vector_writes": bool(mem0.get("direct_vector_writes")),
        },
        "provider": {"ok": provider_ok, "provider_ready": provider_ready, "qdrant_healthy": qdrant_healthy},
        "slo": {
            "ok": slo_ok,
            "status": slo.get("status"),
            "projection_pending": int(observed.get("projection_pending") or 0),
            "projection_failed": int(observed.get("projection_failed") or 0),
            "checks": {key: bool(value) for key, value in checks.items() if key in {"runtime_ready", "cutover_ready", "provider_ready", "qdrant_healthy", "projection_failed_within_budget", "projection_pending_within_budget"}},
        },
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "model_started": False, "raw_source_returned": False},
    }


def _code_tool(client: _LocalRuntimeClient, operation: str, *, project: str, root: str, **extra: Any) -> dict[str, Any]:
    payload = {"operation": operation, "project": project, "root": root, **extra}
    return client.post("/bhm/code-tools", payload)


def _freshness_evidence(
    status: Mapping[str, Any],
    coverage: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    max_age_seconds: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    index = status.get("index") if isinstance(status.get("index"), Mapping) else {}
    current = index.get("current_snapshot") if isinstance(index.get("current_snapshot"), Mapping) else {}
    latest_job = index.get("latest_job") if isinstance(index.get("latest_job"), Mapping) else {}
    graph = status.get("graph") if isinstance(status.get("graph"), Mapping) else {}
    summary = coverage.get("coverage") if isinstance(coverage.get("coverage"), Mapping) else {}
    completed_at = _parse_timestamp(current.get("completed_at") or latest_job.get("completed_at"))
    age = None if completed_at is None else max(0.0, ((_utc_now() if now is None else now) - completed_at).total_seconds())
    graph_summary = graph.get("summary") if isinstance(graph.get("summary"), Mapping) else {}
    parser_errors = int(summary.get("errors") or graph_summary.get("parser_error_count") or 0)
    index_fresh = bool(index.get("fresh")) and bool(coverage.get("index_fresh"))
    complete = bool(summary.get("complete"))
    graph_complete = str(graph.get("status") or "").casefold() == "completed"
    age_ok = age is not None and age <= max(1.0, float(max_age_seconds))
    digest_aligned = bool(
        current.get("snapshot_digest")
        and graph.get("repository_snapshot_id") == current.get("snapshot_id")
        and graph.get("graph_snapshot_id")
        and schema.get("graph_snapshot_id") == graph.get("graph_snapshot_id")
    )
    ok = bool(index_fresh and complete and graph_complete and parser_errors == 0 and age_ok and digest_aligned)
    return {
        "ok": ok,
        "index_fresh": index_fresh,
        "coverage_complete": complete,
        "graph_complete": graph_complete,
        "parser_errors": parser_errors,
        "snapshot_age_seconds": None if age is None else round(age, 3),
        "max_snapshot_age_seconds": round(float(max_age_seconds), 3),
        "age_within_budget": age_ok,
        "snapshot_graph_aligned": digest_aligned,
        "snapshot_id": current.get("snapshot_id"),
        "snapshot_digest": current.get("snapshot_digest"),
        "graph_snapshot_id": graph.get("graph_snapshot_id"),
        "contract_digest": schema.get("contract_digest") or status.get("contract_digest"),
    }


def _live_semantic_evidence(
    client: _LocalRuntimeClient,
    *,
    project: str,
    root: str,
    queries: Sequence[str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for query in list(queries)[:MAX_LIVE_QUERIES]:
        try:
            result = _code_tool(
                client,
                "code_search",
                project=project,
                root=root,
                query=str(query)[:240],
                search_mode="metadata",
                limit=8,
                offset=0,
                include_snippets=False,
                semantic_fusion=True,
                semantic_weight=0.35,
            )
            fusion = result.get("semantic_fusion") if isinstance(result.get("semantic_fusion"), Mapping) else {}
            execution = result.get("execution") if isinstance(result.get("execution"), Mapping) else {}
            rows.append(
                {
                    "query": _bounded_text(query, 120),
                    "request_status": fusion.get("request_status"),
                    "enabled": bool(fusion.get("enabled")),
                    "active": bool(fusion.get("active")),
                    "match_count": len(result.get("matches") or []) if isinstance(result.get("matches"), list) else 0,
                    "embedding_contract": fusion.get("embedding_contract") if isinstance(fusion.get("embedding_contract"), Mapping) else {},
                    "projection_only": execution.get("writes_sqlite_state") is False and execution.get("writes_qdrant") is False and execution.get("raw_source_returned") is False,
                    "error": "",
                }
            )
        except RuntimeError as exc:
            rows.append({"query": _bounded_text(query, 120), "request_status": "error", "enabled": False, "active": False, "match_count": 0, "embedding_contract": {}, "projection_only": False, "error": _safe_error(exc)})
    active = sum(bool(row.get("active")) for row in rows)
    disabled = sum(str(row.get("request_status") or "").casefold() == "feature_disabled" for row in rows)
    errors = sum(str(row.get("request_status") or "").casefold() == "error" for row in rows)
    # This is an evidence report, not a flag-enablement gate.  A disabled
    # feature is explicitly reported as not evaluated rather than masked.
    if errors:
        state = "error"
    elif active:
        state = "active"
    elif disabled == len(rows) and rows:
        state = "disabled"
    else:
        state = "unavailable"
    return {
        "state": state,
        "queries": rows,
        "active_queries": active,
        "disabled_queries": disabled,
        "error_queries": errors,
        "evaluated": bool(active),
        "quality_note": "live semantic quality is not evaluated when the explicit feature flag is disabled or provider fusion is unavailable",
    }


def run_live_audit(
    *,
    base_url: str,
    token: str,
    project: str,
    root: str,
    queries: Sequence[str] = DEFAULT_QUERIES,
    max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
) -> dict[str, Any]:
    """Run bounded live read-only probes against the local BHM runtime."""

    client = _LocalRuntimeClient(base_url, token)
    runtime = _runtime_health(client)
    try:
        status = _code_tool(client, "status", project=project, root=root)
        coverage = _code_tool(client, "coverage", project=project, root=root)
        schema = _code_tool(client, "schema", project=project, root=root)
        freshness = _freshness_evidence(status, coverage, schema, max_age_seconds=max_snapshot_age_seconds)
        semantic = _live_semantic_evidence(client, project=project, root=root, queries=queries)
        error = ""
    except RuntimeError as exc:
        freshness = {"ok": False, "error": _safe_error(exc)}
        semantic = {"state": "unavailable", "queries": [], "evaluated": False, "quality_note": "live semantic quality was not evaluated"}
        error = _safe_error(exc)
    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": "live-read-only",
        "project": project,
        "root": root,
        "runtime": runtime,
        "freshness": freshness,
        "semantic": semantic,
        "error": error,
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "model_started": False, "autonomous_apply": False, "raw_source_returned": False},
    }
    result["ok"] = bool(runtime.get("ok") and freshness.get("ok") and not error)
    result["evidence_digest"] = _sha256({key: value for key, value in result.items() if key not in {"evidence_digest", "ok"}})
    return result


def run_gate(
    *,
    base_url: str | None,
    token: str,
    project: str,
    root: str,
    queries: Sequence[str],
    cases: int,
    max_snapshot_age_seconds: float,
) -> dict[str, Any]:
    benchmark = run_semantic_benchmark(cases)
    live = None
    if base_url:
        live = run_live_audit(
            base_url=base_url,
            token=token,
            project=project,
            root=root,
            queries=queries,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": benchmark,
        "live": live,
        "read_only": True,
        "authority": "sqlite-authoritative",
        "semantic_layer": "mem0-logical",
        "projection_layer": "qdrant-projection-only",
        "provider_health_scope": "runtime/provider/qdrant readiness only; no provider configuration mutation",
    }
    result["ok"] = bool(benchmark.get("ok") and (live is None or live.get("ok")))
    result["evidence_digest"] = _sha256({key: value for key, value in result.items() if key not in {"evidence_digest", "ok"}})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="", help="local BHM URL; omit for offline benchmark only")
    parser.add_argument("--token", default=os.getenv("BHM_CALLER_TOKEN", ""), help="caller token (never printed)")
    parser.add_argument("--project", default="sojmieblo")
    parser.add_argument("--root", default=r"E:\GitHub\repos\sojmieblo")
    parser.add_argument("--query", dest="queries", action="append", default=None)
    parser.add_argument("--cases", type=int, default=16)
    parser.add_argument("--max-snapshot-age-seconds", type=float, default=DEFAULT_MAX_SNAPSHOT_AGE_SECONDS)
    args = parser.parse_args()
    try:
        result = run_gate(
            base_url=args.base_url or None,
            token=args.token,
            project=args.project,
            root=args.root,
            queries=tuple(args.queries or DEFAULT_QUERIES),
            cases=args.cases,
            max_snapshot_age_seconds=args.max_snapshot_age_seconds,
        )
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "ok": False, "error": _safe_error(exc), "read_only": True}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
