"""Run a deterministic P17.4 resource-governor admission drill."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from blackholememory.llm_resource_governor import AdmissionRequest  # noqa: E402
from blackholememory.llm_resource_governor import GovernorConfig  # noqa: E402
from blackholememory.llm_resource_governor import LLMResourceGovernor  # noqa: E402
from blackholememory.llm_resource_governor import MaintenanceWindow  # noqa: E402
from blackholememory.llm_resource_governor import ResourceSnapshot  # noqa: E402
from blackholememory.llm_resource_governor import WorkloadLimits  # noqa: E402
from blackholememory.llm_resource_governor import nvidia_smi_snapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-live-gpu", action="store_true")
    args = parser.parse_args()
    config = GovernorConfig(
        max_concurrency=3,
        interactive_reserve=1,
        maintenance_window=MaintenanceWindow.parse("00:00-06:00"),
        limits={
            "interactive": WorkloadLimits(30, 100),
            "foreground": WorkloadLimits(60, 200),
            "background": WorkloadLimits(90, 300),
        },
    )
    good_gpu = ResourceSnapshot(True, vram_used_mib=4_000, vram_total_mib=12_282, temperature_c=60.0)
    governor = LLMResourceGovernor(config, gpu_probe=lambda: good_gpu)
    now = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
    background = governor.admit(AdmissionRequest("background", "background", 20, 20), now=now)
    foreground = governor.admit(AdmissionRequest("foreground", "foreground", 20, 20), now=now)
    interactive = governor.admit(AdmissionRequest("interactive", "interactive", 20, 20), now=now)
    reserved = governor.admit(AdmissionRequest("reserved", "foreground", 20, 20), now=now)
    governor.set_user_activity(True)
    activity_pause = governor.admit(AdmissionRequest("activity", "background", 20, 20), now=now)
    hot = governor.admit(
        AdmissionRequest("hot", "interactive", 20, 20),
        now=now,
        resources=ResourceSnapshot(True, vram_used_mib=4_000, vram_total_mib=12_282, temperature_c=83.0),
    )
    governor.release("background")
    live_gpu = nvidia_smi_snapshot()
    report = {
        "ok": bool(
            background.allowed
            and foreground.allowed
            and interactive.allowed
            and reserved.code == "interactive_reserve"
            and activity_pause.code == "user_activity_pause"
            and hot.code == "temperature_threshold"
            and (not args.require_live_gpu or live_gpu.gpu_available)
        ),
        "workload_priority": {
            "background": background.priority_rank,
            "foreground": foreground.priority_rank,
            "interactive": interactive.priority_rank,
        },
        "admission": {
            "background": background.code,
            "foreground": foreground.code,
            "interactive": interactive.code,
            "reserved_capacity": reserved.code,
            "user_activity": activity_pause.code,
            "temperature": hot.code,
        },
        "status": governor.status(),
        "live_gpu_probe": live_gpu.as_dict(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
