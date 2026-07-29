"""Run a deterministic P17.6 aggregate telemetry/evaluation drill."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from blackholememory.llm_telemetry import LLMTelemetry  # noqa: E402


def main() -> int:
    telemetry = LLMTelemetry(max_groups=4, max_samples=8)
    for index in range(20):
        telemetry.record(
            job_type="memory-summary" if index % 2 == 0 else "retrieval-rewrite",
            workload="background" if index % 3 else "foreground",
            project="blackholememory",
            status="completed" if index % 5 else "retry",
            queue_wait_ms=10 + index,
            latency_ms=100 + index * 5,
            prompt_tokens=100 + index,
            completion_tokens=20 + index,
            schema_pass=index % 4 != 0,
            validator_pass=index % 5 != 0,
            outcome="accepted" if index % 3 else "rejected",
            retry_count=1 if index % 5 == 0 else 0,
            fallback=index % 7 == 0,
            usefulness="positive" if index % 2 == 0 else "negative",
            gpu_temperature_c=55 + index % 4,
            gpu_vram_used_ratio=0.7,
            gpu_utilization_percent=70 + index % 10,
        )
    telemetry.record_gateway_result(
        SimpleNamespace(
            ok=True,
            latency_ms=200,
            usage={"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25},
            validation={"checked": True, "ok": True},
            failure=None,
            content="raw content is intentionally not persisted",
        ),
        job_type="gateway-probe",
        workload="interactive",
        project="blackholememory",
        outcome="accepted",
    )
    snapshot = telemetry.snapshot()
    serialized = json.dumps(snapshot, ensure_ascii=False)
    report = {
        "ok": bool(
            snapshot["schema_version"] == 1
            and snapshot["totals"]["jobs"] == 21
            and snapshot["totals"]["tokens"]["total"] > 0
            and snapshot["totals"]["schema_pass"] > 0
            and snapshot["totals"]["validator_pass"] > 0
            and snapshot["totals"]["accepted"] > 0
            and snapshot["totals"]["rejected"] > 0
            and snapshot["privacy"]["raw_prompts"] is False
            and snapshot["privacy"]["raw_content"] is False
            and "raw content is intentionally" not in serialized
        ),
        "schema_version": snapshot["schema_version"],
        "jobs": snapshot["totals"]["jobs"],
        "groups": len(snapshot["groups"]),
        "schema_pass": snapshot["totals"]["schema_pass"],
        "validator_pass": snapshot["totals"]["validator_pass"],
        "accepted": snapshot["totals"]["accepted"],
        "rejected": snapshot["totals"]["rejected"],
        "retry_count": snapshot["totals"]["retry_count"],
        "fallback_count": snapshot["totals"]["fallback_count"],
        "privacy": snapshot["privacy"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
