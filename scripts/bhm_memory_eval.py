"""BHM memory search quality evaluation utility."""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

GOLDEN_SCHEMA_VERSION = "bhm.memory-golden.v1"


def _percentile(values: list[float], p: float) -> float:
    """Calculate p-th percentile with linear interpolation."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[int(f)] * (c - k) + sorted_vals[int(c)] * (k - f))


def _sanitize_error(error_text: str, token: str) -> str:
    """Prevent sensitive token string from appearing in error messages."""
    if token and token in error_text:
        return error_text.replace(token, "[REDACTED]")
    return error_text


def make_search_request(
    base_url: str,
    token: str,
    query: str,
    project: str | None,
    limit: int,
    *,
    abstain: bool = False,
    detail: str | None = None,
    timeout: float = 10.0,
) -> tuple[dict[str, Any] | None, int, float, str | None]:
    """Execute search request and return (response_data, response_bytes, latency_ms, error)."""
    url = f"{base_url.rstrip('/')}/bhm/search"
    payload: dict[str, Any] = {
        "query": query,
        "project": project,
        "limit": limit,
    }
    if abstain:
        payload["abstain"] = True
    if detail:
        payload["detail"] = detail

    body_bytes = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-BHM-Caller-Surface": "launcher",
    }
    if project:
        headers["X-BHM-Caller-Project"] = str(project)

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    start_time = time.perf_counter()
    resp_bytes = b""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_bytes = resp.read()
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            data = json.loads(resp_bytes.decode("utf-8"))
            if not isinstance(data, dict):
                return None, len(resp_bytes), latency_ms, "Invalid JSON: response is not an object"
            return data, len(resp_bytes), latency_ms, None
    except urllib.error.HTTPError as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        try:
            err_body = exc.read()
            byte_len = len(err_body)
        except Exception:
            byte_len = 0
        return None, byte_len, latency_ms, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        reason_str = _sanitize_error(str(exc.reason), token)
        if "timed out" in reason_str.lower():
            err_msg = "Timeout"
        else:
            err_msg = f"Connection error: {reason_str}"
        return None, 0, latency_ms, err_msg
    except (TimeoutError, socket.timeout):
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        return None, 0, latency_ms, "Timeout"
    except json.JSONDecodeError:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        return None, len(resp_bytes), latency_ms, "Invalid JSON in response"
    except Exception as exc:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        return None, 0, latency_ms, _sanitize_error(f"Request failed: {exc.__class__.__name__}", token)


def evaluate_item(
    item: dict[str, Any],
    response_data: dict[str, Any] | None,
    response_bytes: int,
    latency_ms: float,
    error: str | None,
    top: int,
) -> dict[str, Any]:
    """Score single question against golden expectations and response payload."""
    item_id = str(item.get("id") or "")
    kind = str(item.get("kind") or "unknown")
    expect_route = item.get("expect_route")
    expect_none = bool(item.get("expect_none", False))
    expect_any = item.get("expect_any")

    is_route = bool(expect_route)
    is_expect_none = expect_none
    is_expect_any = bool(expect_any and not is_route and not is_expect_none)

    top_memories: list[dict[str, Any]] = []
    best_score: float | None = None
    route_hint: str | None = None
    rank: int | None = None
    passed = False

    if response_data is not None and error is None:
        memories = response_data.get("memories")
        if not isinstance(memories, list):
            memories = []

        route_hint = response_data.get("suggested_source")

        for m in memories[:3]:
            if isinstance(m, dict):
                top_memories.append({
                    "id": m.get("id"),
                    "title": m.get("title"),
                })

        if memories and isinstance(memories[0], dict):
            raw_score = memories[0].get("score")
            if isinstance(raw_score, (int, float)):
                best_score = float(raw_score)

        if is_route:
            # Route questions are counted separately and do not enter main pass score
            if expect_route and route_hint:
                passed = str(route_hint).strip().lower() == str(expect_route).strip().lower()
            else:
                passed = False
        elif is_expect_none:
            ans = response_data.get("answerability")
            if isinstance(ans, dict) and "status" in ans:
                status_str = str(ans.get("status") or "").strip().lower()
                if status_str in ("none", "weak") or len(memories) == 0:
                    passed = True
                else:
                    passed = False
            else:
                # Legacy response lacking answerability key: passes only if memories list is empty
                passed = len(memories) == 0
        else:
            candidates = [str(x) for x in (expect_any or [])]
            found_rank: int | None = None
            for idx, mem in enumerate(memories):
                if not isinstance(mem, dict):
                    continue
                m_id = str(mem.get("id") or "")
                m_title = str(mem.get("title") or "")
                m_content = str(mem.get("content") or mem.get("snippet") or "")

                matched = False
                for cand in candidates:
                    cand_lower = cand.lower()
                    if (
                        cand_lower == m_id.lower()
                        or cand_lower in m_id.lower()
                        or cand_lower in m_title.lower()
                        or cand_lower in m_content.lower()
                    ):
                        matched = True
                        break
                if matched:
                    found_rank = idx + 1
                    break

            rank = found_rank
            passed = found_rank is not None and found_rank <= top
    else:
        passed = False

    return {
        "id": item_id,
        "kind": kind,
        "is_route": is_route,
        "is_expect_none": is_expect_none,
        "is_expect_any": is_expect_any,
        "passed": passed,
        "rank": rank,
        "best_score": best_score,
        "response_bytes": response_bytes,
        "latency_ms": latency_ms,
        "top_memories": top_memories,
        "route_hint": route_hint,
        "error": error,
    }


def compute_metrics(results: list[dict[str, Any]], top: int) -> dict[str, Any]:
    """Calculate aggregate metrics across evaluated and route items."""
    eval_items = [it for it in results if not it["is_route"]]
    route_items = [it for it in results if it["is_route"]]

    expect_any_items = [it for it in eval_items if it["is_expect_any"]]
    expect_none_items = [it for it in eval_items if it["is_expect_none"]]

    pass_at_1_count = sum(1 for it in expect_any_items if it["rank"] == 1)
    pass_at_1 = (pass_at_1_count / len(expect_any_items)) if expect_any_items else 0.0

    pass_at_top_count = sum(1 for it in expect_any_items if it["passed"])
    pass_at_top = (pass_at_top_count / len(expect_any_items)) if expect_any_items else 0.0

    none_passed_count = sum(1 for it in expect_none_items if it["passed"])
    correct_refusal_rate = (none_passed_count / len(expect_none_items)) if expect_none_items else 0.0

    eval_passed_count = sum(1 for it in eval_items if it["passed"])
    overall_pass_rate = (eval_passed_count / len(eval_items)) if eval_items else 0.0

    request_errors = sum(1 for it in results if it["error"] is not None)

    latencies = [it["latency_ms"] for it in results]
    sizes = [it["response_bytes"] for it in results]

    mean_latency = statistics.mean(latencies) if latencies else 0.0
    p95_latency = _percentile(latencies, 95) if latencies else 0.0

    mean_bytes = statistics.mean(sizes) if sizes else 0.0
    p95_bytes = _percentile(sizes, 95) if sizes else 0.0

    # Metrics by question kind
    by_kind: dict[str, Any] = {}
    distinct_kinds = sorted({it["kind"] for it in eval_items})
    for kind in distinct_kinds:
        kind_items = [it for it in eval_items if it["kind"] == kind]
        k_any = [it for it in kind_items if it["is_expect_any"]]
        k_none = [it for it in kind_items if it["is_expect_none"]]
        k_total = len(kind_items)
        k_passed = sum(1 for it in kind_items if it["passed"])

        p1_rate = (sum(1 for it in k_any if it["rank"] == 1) / len(k_any)) if k_any else None
        pt_rate = (sum(1 for it in k_any if it["passed"]) / len(k_any)) if k_any else None
        ref_rate = (sum(1 for it in k_none if it["passed"]) / len(k_none)) if k_none else None

        by_kind[kind] = {
            "total": k_total,
            "passed": k_passed,
            "pass_rate": round(k_passed / k_total, 4) if k_total else 0.0,
            "pass_at_1": round(p1_rate, 4) if p1_rate is not None else None,
            "pass_at_top": round(pt_rate, 4) if pt_rate is not None else None,
            "correct_refusal_rate": round(ref_rate, 4) if ref_rate is not None else None,
            "request_errors": sum(1 for it in kind_items if it["error"] is not None),
        }

    route_total = len(route_items)
    route_passed = sum(1 for it in route_items if it["passed"])
    route_pass_rate = (route_passed / route_total) if route_total > 0 else 0.0

    return {
        "total_questions": len(results),
        "evaluated_questions": len(eval_items),
        "route_questions": route_total,
        "top_k": top,
        "pass_at_1": round(pass_at_1, 4),
        "pass_at_top": round(pass_at_top, 4),
        f"pass_at_{top}": round(pass_at_top, 4),
        "expect_none_total": len(expect_none_items),
        "expect_none_passed": none_passed_count,
        "correct_refusal_rate": round(correct_refusal_rate, 4),
        "overall_passed": eval_passed_count,
        "overall_pass_rate": round(overall_pass_rate, 4),
        "request_errors": request_errors,
        "latency_ms": {
            "mean": round(mean_latency, 2),
            "p95": round(p95_latency, 2),
        },
        "response_bytes": {
            "mean": round(mean_bytes, 2),
            "p95": round(p95_bytes, 2),
        },
        "by_kind": by_kind,
        "route_items": {
            "total": route_total,
            "passed": route_passed,
            "pass_rate": round(route_pass_rate, 4),
        },
    }


def format_russian_table(summary: dict[str, Any]) -> str:
    """Format evaluation summary into Russian human-readable CLI table."""
    lines: list[str] = [
        "=" * 64,
        "Сводка оценки качества поиска памяти BHM",
        "=" * 64,
        f"Всего вопросов:          {summary.get('total_questions', 0)}",
        f"Оценено вопросов:        {summary.get('evaluated_questions', 0)}",
        f"Route-вопросов:          {summary.get('route_questions', 0)}",
        f"Ошибок запросов:         {summary.get('request_errors', 0)}",
        "",
    ]
    top_k = summary.get("top_k", 3)
    p1 = summary.get("pass_at_1", 0.0) * 100
    pt = summary.get("pass_at_top", 0.0) * 100
    ref_rate = summary.get("correct_refusal_rate", 0.0) * 100
    none_passed = summary.get("expect_none_passed", 0)
    none_total = summary.get("expect_none_total", 0)

    lines.extend([
        "Метрики качества:",
        f"  pass@1:                {p1:.1f}%",
        f"  pass@{top_k}:                {pt:.1f}%",
        f"  Корректные отказы:     {ref_rate:.1f}% ({none_passed}/{none_total})",
        "",
    ])

    lat = summary.get("latency_ms", {})
    sz = summary.get("response_bytes", {})
    lines.extend([
        "Производительность:",
        f"  Задержка (мс):         средняя: {lat.get('mean', 0.0):.1f}, p95: {lat.get('p95', 0.0):.1f}",
        f"  Размер ответа (байт):  средний: {sz.get('mean', 0.0):.1f}, p95: {sz.get('p95', 0.0):.1f}",
        "",
    ])

    by_kind = summary.get("by_kind", {})
    if by_kind:
        lines.append("Разрез по категориям (kind):")
        lines.append(f"  {'Категория':<16} {'Всего':>6} {'Пройдено':>9} {'pass@1':>9} {f'pass@{top_k}':>9} {'Отказы':>9}")
        lines.append("  " + "-" * 60)
        for kind_name, stats in sorted(by_kind.items()):
            tot = stats.get("total", 0)
            psd = stats.get("passed", 0)
            p1_str = f"{stats['pass_at_1'] * 100:.1f}%" if stats.get("pass_at_1") is not None else "-"
            pt_str = f"{stats['pass_at_top'] * 100:.1f}%" if stats.get("pass_at_top") is not None else "-"
            ref_str = f"{stats['correct_refusal_rate'] * 100:.1f}%" if stats.get("correct_refusal_rate") is not None else "-"
            lines.append(f"  {kind_name:<16} {tot:>6} {psd:>9} {p1_str:>9} {pt_str:>9} {ref_str:>9}")
        lines.append("")

    route_stats = summary.get("route_items", {})
    if route_stats.get("total", 0) > 0:
        lines.append("Route-вопросы (вне основного зачёта):")
        r_tot = route_stats.get("total", 0)
        r_psd = route_stats.get("passed", 0)
        r_rate = route_stats.get("pass_rate", 0.0) * 100
        lines.append(f"  Всего: {r_tot}, совпало: {r_psd} ({r_rate:.1f}%)")
        lines.append("")

    lines.append("=" * 64)
    return "\n".join(lines)


def run_evaluation(
    golden_data: dict[str, Any],
    *,
    base_url: str,
    token: str,
    top: int = 3,
    limit: int = 10,
    abstain: bool = False,
    detail: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Run full evaluation over golden questions dataset."""
    items = golden_data.get("items") or []
    item_results: list[dict[str, Any]] = []

    for item in items:
        query = str(item.get("q") or "")
        project = item.get("project")
        resp_data, resp_bytes, latency, error = make_search_request(
            base_url=base_url,
            token=token,
            query=query,
            project=project,
            limit=limit,
            abstain=abstain,
            detail=detail,
            timeout=timeout,
        )
        scored = evaluate_item(
            item=item,
            response_data=resp_data,
            response_bytes=resp_bytes,
            latency_ms=latency,
            error=error,
            top=top,
        )
        item_results.append(scored)

    summary = compute_metrics(item_results, top=top)

    clean_items: list[dict[str, Any]] = []
    for it in item_results:
        clean_items.append({
            "id": it["id"],
            "kind": it["kind"],
            "passed": it["passed"],
            "rank": it["rank"],
            "best_score": it["best_score"],
            "response_bytes": it["response_bytes"],
            "latency_ms": round(it["latency_ms"], 2),
            "top_memories": it["top_memories"],
            "route_hint": it["route_hint"],
            "error": it["error"],
        })

    return {
        "summary": summary,
        "items": clean_items,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="BHM memory search quality evaluation utility."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL for BHM service")
    parser.add_argument("--golden", required=True, help="Path to golden questions JSON file")
    parser.add_argument("--out", default=None, help="Path to output JSON evaluation report")
    parser.add_argument("--top", type=int, default=3, help="Top-K cutoff for pass@K (default: 3)")
    parser.add_argument("--limit", type=int, default=10, help="Search memory limit (default: 10)")
    parser.add_argument("--abstain", action="store_true", default=False, help="Enable abstain in requests")
    parser.add_argument("--detail", choices=["compact", "full"], default=None, help="Detail mode (compact/full)")
    parser.add_argument("--timeout", type=float, default=10.0, help="Request timeout in seconds (default: 10.0)")
    parser.add_argument("--token-env", default="BHM_CALLER_TOKEN", help="Env variable name containing caller token")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    golden_path = Path(args.golden)
    if not golden_path.is_file():
        sys.stderr.write(f"Error: Golden file not accessible: {args.golden}\n")
        return 2

    try:
        content = golden_path.read_text(encoding="utf-8")
        golden_data = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"Error reading golden file: {exc}\n")
        return 2

    if not isinstance(golden_data, dict) or golden_data.get("schema") != GOLDEN_SCHEMA_VERSION:
        sys.stderr.write(f"Error: Golden file must have schema {GOLDEN_SCHEMA_VERSION!r}\n")
        return 2

    if not isinstance(golden_data.get("items"), list):
        sys.stderr.write("Error: Golden file 'items' must be a list\n")
        return 2

    token = os.environ.get(args.token_env)
    if not token:
        sys.stderr.write(f"Error: Token environment variable {args.token_env!r} is not set or empty\n")
        return 2

    report = run_evaluation(
        golden_data,
        base_url=args.base_url,
        token=token,
        top=args.top,
        limit=args.limit,
        abstain=args.abstain,
        detail=args.detail,
        timeout=args.timeout,
    )

    if args.out:
        out_path = Path(args.out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"Error writing output file: {exc}\n")
            return 2

    table_text = format_russian_table(report["summary"])
    print(table_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
