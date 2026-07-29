"""Synthetic WI-05 session capture/progressive-disclosure benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from blackholememory.session_capture import build_session_capture_preview


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _fixture(items_per_source: int) -> tuple[list[dict], list[dict], list[dict]]:
    observations = [
        {
            "eventId": f"evt-{index:04d}",
            "sessionId": "benchmark-session",
            "project": "fixture",
            "hookType": "tool.complete" if index % 2 else "tool.start",
            "timestamp": "2026-07-16T11:59:00Z",
            "data": {"result": "ok", "secret": "redact-me"},
            "recordSha256": hashlib.sha256(f"evt-{index}".encode()).hexdigest(),
        }
        for index in range(items_per_source)
    ]
    sessions = [
        {
            "id": "session-record-benchmark",
            "project": "fixture",
            "session_id": "benchmark-session",
            "title": "benchmark",
            "next": "continue",
            "metadata": {"session_id": "benchmark-session"},
        }
    ]
    memories = [
        {
            "source_id": f"mem-{index:04d}",
            "project": "fixture",
            "memory_type": "fact" if index % 2 else "decision",
            "content": f"durable fact {index}",
            "tags": ["benchmark", "architecture" if index % 2 else "workflow"],
            "updated_at": "2026-07-16T11:58:00Z",
        }
        for index in range(items_per_source)
    ]
    return observations, sessions, memories


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items-per-source", type=int, default=20)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--p95-budget-ms", type=float, default=250.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    observations, sessions, memories = _fixture(max(1, min(args.items_per_source, 96)))
    before = hashlib.sha256(json.dumps([observations, sessions, memories], sort_keys=True).encode()).hexdigest()
    digests: list[str] = []
    durations: list[float] = []
    last: dict = {}
    for _ in range(max(1, args.iterations)):
        started = time.perf_counter()
        last = build_session_capture_preview(
            observations,
            session_records=sessions,
            memories=memories,
            project="fixture",
            session_id="benchmark-session",
            disclosure="audit",
            token_budget=1_200,
            max_items=32,
            now=NOW,
        )
        durations.append((time.perf_counter() - started) * 1000.0)
        digests.append(str(last["response_digest"]))
    after = hashlib.sha256(json.dumps([observations, sessions, memories], sort_keys=True).encode()).hexdigest()
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    checks = {
        "deterministic_digest": len(set(digests)) == 1,
        "p95_budget": p95 <= args.p95_budget_ms,
        "no_input_mutation": before == after,
        "redaction_boundary": last["packet"]["diagnostics"]["raw_payload_returned"] is False,
        "provenance": bool(last["packet"]["provenance"]["observation_event_ids"]),
        "preview_only": last["execution"]["preview_only"] is True and last["execution"]["writes_sqlite"] is False,
    }
    report = {
        "schema_version": "bhm.wi05.session-capture-benchmark.v1",
        "ok": all(checks.values()),
        "fixture": {"items_per_source": len(observations), "session_records": len(sessions), "memories": len(memories)},
        "iterations": len(durations),
        "latency": {"sample_count": len(durations), "p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3)},
        "budget": last.get("budget", {}),
        "checks": checks,
        "response_digest": last.get("response_digest"),
        "writes_live_state": False,
        "model_started": False,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
