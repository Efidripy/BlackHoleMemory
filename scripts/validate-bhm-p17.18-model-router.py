"""Deterministic offline gate for the P17.18 capability-based model router."""

from __future__ import annotations

import json

from blackholememory.model_router import MODEL_ROUTER_SCHEMA_VERSION
from blackholememory.model_router import route_model
from blackholememory.model_router import router_snapshot


def main() -> int:
    local = route_model("code_review", required_capabilities=["coding", "reasoning"], context_tokens=8192)
    unmeasured = route_model("reasoning", required_capabilities=["reasoning"], context_tokens=16384)
    measured = route_model(
        "reasoning",
        required_capabilities=["reasoning"],
        context_tokens=16384,
        measurements=[{"context_tokens": 16384, "ok": True, "latency_ms": 900, "tokens_per_second": 20}],
    )
    vision = route_model("image_review", required_capabilities=["vision"], context_tokens=8192)
    snapshot = router_snapshot()
    checks = {
        "schema": snapshot["schema_version"] == MODEL_ROUTER_SCHEMA_VERSION,
        "local_8k": local.status == "routed" and local.model_id == "qwen2.5-coder-7b-instruct",
        "unmeasured_fail_closed": unmeasured.status == "rejected" and "context_profile_not_measured" in unmeasured.reason_codes,
        "measured_16k": measured.status == "routed" and measured.profile_tokens == 16384,
        "vision_fail_closed": vision.status == "rejected" and "vision_capability_unconfirmed" in vision.reason_codes,
        "profiles": [item["profile_tokens"] for item in snapshot["context_profiles"]] == [8192, 16384, 32768],
        "no_cloud": snapshot["cloud_fallback"] is False and snapshot["local_only_required"] is True,
        "execution_off": snapshot["execution_enabled"] is False and snapshot["auto_apply"] is False,
    }
    report = {
        "ok": all(checks.values()),
        "schema_version": snapshot["schema_version"],
        "routes": {"local_8k": local.as_dict(), "unmeasured_16k": unmeasured.as_dict(), "measured_16k": measured.as_dict(), "vision": vision.as_dict()},
        "checks": checks,
        "execution_enabled": False,
        "auto_apply": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
