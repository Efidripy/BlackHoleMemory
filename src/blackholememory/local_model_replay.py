"""Read-only local-model replay over the BHM value-benchmark fixture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import statistics
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .llm_gateway import is_local_endpoint
from .llm_gateway import normalize_json_content
from .value_benchmark import DEFAULT_CASE_COUNT
from .value_benchmark import DEFAULT_REPEAT_COUNT
from .value_benchmark import ValueBenchmarkCase
from .value_benchmark import build_value_benchmark_cases
from .value_benchmark import build_value_benchmark_context
from .value_benchmark import value_benchmark_fixture_digest


LOCAL_MODEL_REPLAY_SCHEMA_VERSION = "bhm.local-model-replay.v1"
DEFAULT_BASE_URL = "http://127.0.0.1:13666/v1"
DEFAULT_MODEL_ID = "qwen2.5-coder-7b-instruct"
DEFAULT_MODES = ("file-only", "bhm-full")
PROMPT_ID = "bhm-agent-replay"
PROMPT_VERSION = "1.0"
MAX_FAILURE_SAMPLES = 24

SYSTEM_PROMPT = """You are a local, evidence-constrained software agent.
Use only the supplied CONTEXT. Do not use outside knowledge.
Return exactly one JSON object with these keys:
selected_source_id: string, cited_source_ids: array of strings,
scope_ok: boolean, answer: short string.
Select the best source id for the task. Cite only source ids present in CONTEXT.
If CONTEXT contains mixed-project, archived, or log records, do not cite them.
Tool budget is zero: do not invent tool calls."""


async def run_local_model_replay(
    *,
    cases: Sequence[ValueBenchmarkCase] | None = None,
    case_count: int = DEFAULT_CASE_COUNT,
    repeats: int = DEFAULT_REPEAT_COUNT,
    base_url: str = DEFAULT_BASE_URL,
    model_id: str = DEFAULT_MODEL_ID,
    modes: Sequence[str] = DEFAULT_MODES,
    max_in_flight: int = 2,
    max_tokens: int = 96,
    tool_budget: int = 0,
    timeout_seconds: float = 90.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run fixed-prompt model calls and score them against hidden case labels."""

    if not is_local_endpoint(base_url):
        raise ValueError("local-model replay requires a loopback/private endpoint")
    repeat_count = int(repeats)
    if not 1 <= repeat_count <= 100:
        raise ValueError("repeats must be between 1 and 100")
    concurrency = int(max_in_flight)
    if not 1 <= concurrency <= 8:
        raise ValueError("max_in_flight must be between 1 and 8")
    output_tokens = int(max_tokens)
    if not 16 <= output_tokens <= 256:
        raise ValueError("max_tokens must be between 16 and 256")
    if int(tool_budget) != 0:
        raise ValueError("this replay is context-only and requires tool_budget=0")
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be numeric") from None
    if timeout <= 0:
        raise ValueError("timeout_seconds must be positive")

    selected_modes = tuple(dict.fromkeys(str(mode) for mode in modes))
    unsupported = sorted(set(selected_modes) - {"file-only", "bhm-full"})
    if unsupported:
        raise ValueError(f"unsupported local replay modes: {', '.join(unsupported)}")
    if not selected_modes:
        raise ValueError("at least one local replay mode is required")

    fixture = list(cases) if cases is not None else build_value_benchmark_cases(case_count)
    if not fixture:
        raise ValueError("local-model replay requires at least one case")
    fixture_digest = value_benchmark_fixture_digest(fixture)

    runs: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        available_models = await _discover_models(client, model_id)
        for repetition in range(1, repeat_count + 1):
            run_started = time.perf_counter()
            mode_reports: dict[str, dict[str, Any]] = {}
            for mode in selected_modes:
                mode_reports[mode] = await _run_mode(
                    client,
                    fixture,
                    repetition=repetition,
                    mode=mode,
                    model_id=model_id,
                    max_in_flight=concurrency,
                    max_tokens=output_tokens,
                    tool_budget=int(tool_budget),
                    timeout_seconds=timeout,
                )
            runs.append(
                {
                    "repetition": repetition,
                    "modes": mode_reports,
                    "runner_wall_ms": round((time.perf_counter() - run_started) * 1000.0, 3),
                }
            )

    aggregates = {mode: _aggregate_mode(runs, mode) for mode in selected_modes}
    stable_aggregates = {
        mode: {key: value for key, value in aggregate.items() if not _is_timing_key(key)}
        for mode, aggregate in aggregates.items()
    }
    core = {
        "schema_version": LOCAL_MODEL_REPLAY_SCHEMA_VERSION,
        "benchmark": "BHM Local Model Replay v1",
        "case_count": len(fixture),
        "repeat_count": repeat_count,
        "fixture_digest": fixture_digest,
        "modes": list(selected_modes),
        "model": {
            "base_url": _public_base_url(base_url),
            "model_id": model_id,
            "available_models": available_models,
        },
        "fixed_contract": {
            "prompt_id": PROMPT_ID,
            "prompt_version": PROMPT_VERSION,
            "temperature": 0.0,
            "max_tokens": output_tokens,
            "tool_budget": int(tool_budget),
            "max_in_flight": concurrency,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        "aggregates": stable_aggregates,
        "execution": {
            "model_called": True,
            "agent_started": True,
            "network_called": True,
            "sqlite_written": False,
            "qdrant_written": False,
            "mem0_written": False,
            "live_bhm_runtime_used": False,
            "tool_calls_allowed": False,
        },
        "evidence_class": "local-model-replay",
        "limitations": [
            "the model receives frozen contexts and cannot call BHM tools",
            "task success is scored against fixture labels, not human preference",
            "results are specific to the pinned local model, prompt and machine",
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


def render_local_model_replay_markdown(report: Mapping[str, Any]) -> str:
    aggregates = report.get("aggregates") or {}
    model = report.get("model") or {}
    fixed = report.get("fixed_contract") or {}
    lines = [
        "## BHM Local Model Replay",
        "",
        f"Local model: `{model.get('model_id')}` at `{model.get('base_url')}`. "
        f"Workload: {report.get('case_count')} cases × {report.get('repeat_count')} repetitions. "
        f"Evidence class: `{report.get('evidence_class')}`.",
        "",
        f"Fixed contract: prompt `{fixed.get('prompt_id')}@{fixed.get('prompt_version')}`, "
        f"temperature `{fixed.get('temperature')}`, max tokens `{fixed.get('max_tokens')}`, "
        f"tool budget `{fixed.get('tool_budget')}`, thinking disabled.",
        "",
        "| Mode | Model task success | JSON validity | Target selected | Citation validity | Forbidden citations | Model p95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode in report.get("modes") or []:
        row = aggregates[mode]
        lines.append(
            f"| `{mode}` | {_pct(row['task_success_rate'])} | {_pct(row['json_validity'])} | "
            f"{_pct(row['target_selected_rate'])} | {_pct(row['citation_validity'])} | "
            f"{row['forbidden_citation_count']:.0f} | {row['model_latency_ms_p95']:.1f} |"
        )
    lines.extend(
        [
            "",
            f"Fixture digest: `{report.get('fixture_digest')}`  ",
            f"Report digest: `{report.get('report_digest')}`",
            "",
            "> This is a local model replay with frozen contexts and zero tool calls. It is not real-user telemetry.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_local_model_replay_report(report: Mapping[str, Any], output_json: Path, output_markdown: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_markdown.write_text(render_local_model_replay_markdown(report), encoding="utf-8")


async def _discover_models(client: httpx.AsyncClient, model_id: str) -> list[str]:
    response = await client.get("/models")
    response.raise_for_status()
    body = response.json()
    models = body.get("data") if isinstance(body, dict) else []
    ids = sorted({str(item.get("id")) for item in models if isinstance(item, dict) and item.get("id")})
    if model_id not in ids:
        raise RuntimeError(f"local model is not available: {model_id}; available={ids}")
    return ids


async def _run_mode(
    client: httpx.AsyncClient,
    cases: Sequence[ValueBenchmarkCase],
    *,
    repetition: int,
    mode: str,
    model_id: str,
    max_in_flight: int,
    max_tokens: int,
    tool_budget: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(max_in_flight)
    started = time.perf_counter()

    async def run_case(case: ValueBenchmarkCase) -> dict[str, Any]:
        context = build_value_benchmark_context(case, mode)
        user_prompt = _user_prompt(case, context)
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "response_format": {"type": "text"},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request_started = time.perf_counter()
        try:
            async with semaphore:
                response = await client.post("/chat/completions", json=payload, timeout=timeout_seconds)
            response.raise_for_status()
            body = response.json()
            content = _response_content(body)
            parsed = normalize_json_content(content)
            scored = _score_response(case, context, parsed)
            usage = body.get("usage") if isinstance(body, dict) and isinstance(body.get("usage"), dict) else {}
            return {
                **scored,
                "ok": True,
                "latency_ms": round((time.perf_counter() - request_started) * 1000.0, 3),
                "prompt_tokens": _usage_int(usage, "prompt_tokens"),
                "completion_tokens": _usage_int(usage, "completion_tokens"),
                "total_tokens": _usage_int(usage, "total_tokens"),
                "failure_code": "",
            }
        except Exception as exc:
            return {
                "ok": False,
                "json_valid": False,
                "target_selected": False,
                "task_success": False,
                "citation_valid": False,
                "forbidden_citations": 0,
                "latency_ms": round((time.perf_counter() - request_started) * 1000.0, 3),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "failure_code": type(exc).__name__,
            }

    results = await asyncio.gather(*(run_case(case) for case in cases))
    return {
        "repetition": repetition,
        "cases": len(cases),
        "call_count": len(results),
        "runner_wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
        **_summarize_results(results),
    }


def _user_prompt(case: ValueBenchmarkCase, context: Mapping[str, Any]) -> str:
    compiled = context.get("compiled") if isinstance(context.get("compiled"), Mapping) else {}
    context_text = str(compiled.get("text") or "[EMPTY CONTEXT]")
    return f"Project: {case.project}\nTask: {case.query}\nCONTEXT:\n{context_text}"


def _response_content(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices") if isinstance(body.get("choices"), list) else []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    return content if isinstance(content, str) else str(content or "")


def _score_response(case: ValueBenchmarkCase, context: Mapping[str, Any], parsed: dict[str, Any] | None) -> dict[str, Any]:
    valid = isinstance(parsed, dict) and {"selected_source_id", "cited_source_ids", "scope_ok", "answer"}.issubset(parsed)
    if not valid:
        return {
            "json_valid": False,
            "target_selected": False,
            "task_success": False,
            "citation_valid": False,
            "forbidden_citations": 0,
        }
    selected = str(parsed.get("selected_source_id") or "")
    cited = parsed.get("cited_source_ids") if isinstance(parsed.get("cited_source_ids"), list) else []
    cited_ids = {str(item) for item in cited if str(item)}
    allowed_ids = {str(item) for item in context.get("allowed_ids") or []}
    forbidden_ids = {str(item) for item in context.get("forbidden_ids") or []}
    citation_valid = bool(cited_ids) and cited_ids.issubset(allowed_ids)
    forbidden_citations = len(cited_ids & forbidden_ids)
    target_selected = selected == case.target_id
    task_success = target_selected and case.target_id in cited_ids and citation_valid and parsed.get("scope_ok") is True
    return {
        "json_valid": True,
        "target_selected": target_selected,
        "task_success": task_success,
        "citation_valid": citation_valid,
        "forbidden_citations": forbidden_citations,
    }


def _summarize_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    count = max(len(results), 1)
    latencies = [float(item.get("latency_ms") or 0.0) for item in results]
    failure_codes = Counter(str(item.get("failure_code") or "") for item in results if not item.get("ok"))
    return {
        "json_validity": round(sum(bool(item.get("json_valid")) for item in results) / count, 6),
        "target_selected_rate": round(sum(bool(item.get("target_selected")) for item in results) / count, 6),
        "task_success_rate": round(sum(bool(item.get("task_success")) for item in results) / count, 6),
        "citation_validity": round(sum(bool(item.get("citation_valid")) for item in results) / count, 6),
        "forbidden_citation_count": sum(int(item.get("forbidden_citations") or 0) for item in results),
        "model_latency_ms_mean": round(statistics.fmean(latencies), 6),
        "model_latency_ms_p50": round(_percentile(latencies, 0.50), 6),
        "model_latency_ms_p95": round(_percentile(latencies, 0.95), 6),
        "prompt_tokens": sum(int(item.get("prompt_tokens") or 0) for item in results),
        "completion_tokens": sum(int(item.get("completion_tokens") or 0) for item in results),
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in results),
        "failed_calls": sum(not bool(item.get("ok")) for item in results),
        "failure_codes": dict(sorted(failure_codes.items())),
    }


def _aggregate_mode(runs: Sequence[Mapping[str, Any]], mode: str) -> dict[str, Any]:
    metric_names = (
        "json_validity",
        "target_selected_rate",
        "task_success_rate",
        "citation_validity",
        "forbidden_citation_count",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "failed_calls",
    )
    values = {name: [float(run["modes"][mode][name]) for run in runs] for name in metric_names}
    latencies = [float(run["modes"][mode]["model_latency_ms_p95"]) for run in runs]
    result: dict[str, Any] = {"repetitions": len(runs), "cases": int(runs[0]["modes"][mode]["cases"])}
    for name, samples in values.items():
        result[name] = round(statistics.fmean(samples), 6)
        result[f"{name}_min"] = round(min(samples), 6)
        result[f"{name}_max"] = round(max(samples), 6)
    result["model_latency_ms_p95"] = round(statistics.fmean(latencies), 6)
    result["model_latency_ms_p95_min"] = round(min(latencies), 6)
    result["model_latency_ms_p95_max"] = round(max(latencies), 6)
    result["failure_codes"] = dict(sorted(Counter(code for run in runs for code, count in (run["modes"][mode].get("failure_codes") or {}).items() for _ in range(int(count))).items()))
    return result


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _usage_int(usage: Mapping[str, Any], key: str) -> int:
    try:
        return max(int(usage.get(key) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _is_timing_key(key: str) -> bool:
    return key.startswith("model_latency_ms") or key.endswith("wall_ms")


def _public_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}{parsed.path.rstrip('/')}"


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _pct(value: float) -> str:
    return f"{float(value) * 100:.1f}%"


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL_ID",
    "DEFAULT_MODES",
    "LOCAL_MODEL_REPLAY_SCHEMA_VERSION",
    "render_local_model_replay_markdown",
    "run_local_model_replay",
    "write_local_model_replay_report",
]
