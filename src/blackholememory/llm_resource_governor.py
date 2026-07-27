"""Admission and resource policy for local-LLM work.

The governor is intentionally execution-free: it reserves bounded capacity and
returns an explainable decision. A worker remains responsible for enforcing the
returned wall-time/output limits around the actual gateway call.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any, Callable, Literal


LLMWorkload = Literal["interactive", "foreground", "background"]
WORKLOAD_PRIORITY: dict[LLMWorkload, int] = {
    "interactive": 0,
    "foreground": 1,
    "background": 2,
}


class LLMResourceGovernorError(ValueError):
    pass


@dataclass(frozen=True)
class MaintenanceWindow:
    start_minute: int
    end_minute: int

    @classmethod
    def parse(cls, value: str) -> "MaintenanceWindow | None":
        text = str(value or "").strip()
        if not text:
            return None
        try:
            start_text, end_text = text.split("-", 1)
            start = _parse_minute(start_text)
            end = _parse_minute(end_text)
        except (ValueError, IndexError) as exc:
            raise LLMResourceGovernorError(f"invalid maintenance window: {value!r}") from exc
        return cls(start_minute=start, end_minute=end)

    def contains(self, current: datetime) -> bool:
        minute = current.hour * 60 + current.minute
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        if self.start_minute > self.end_minute:
            return minute >= self.start_minute or minute < self.end_minute
        return False

    def as_text(self) -> str:
        return f"{self.start_minute // 60:02d}:{self.start_minute % 60:02d}-{self.end_minute // 60:02d}:{self.end_minute % 60:02d}"


@dataclass(frozen=True)
class WorkloadLimits:
    max_wall_seconds: float
    max_output_tokens: int


@dataclass(frozen=True)
class GovernorConfig:
    max_concurrency: int = 2
    interactive_reserve: int = 1
    max_temperature_c: float = 82.0
    max_vram_used_ratio: float = 0.92
    pause_on_user_activity: bool = True
    background_requires_maintenance_window: bool = True
    require_gpu_probe: bool = True
    maintenance_window: MaintenanceWindow | None = None
    limits: dict[LLMWorkload, WorkloadLimits] = field(
        default_factory=lambda: {
            "interactive": WorkloadLimits(max_wall_seconds=120.0, max_output_tokens=1_024),
            "foreground": WorkloadLimits(max_wall_seconds=300.0, max_output_tokens=2_048),
            "background": WorkloadLimits(max_wall_seconds=900.0, max_output_tokens=4_096),
        }
    )

    def __post_init__(self) -> None:
        if int(self.max_concurrency) < 1:
            raise LLMResourceGovernorError("max_concurrency must be positive")
        if not 0 <= int(self.interactive_reserve) < int(self.max_concurrency):
            raise LLMResourceGovernorError("interactive_reserve must be >= 0 and lower than max_concurrency")
        if not 0 < float(self.max_temperature_c) <= 120:
            raise LLMResourceGovernorError("max_temperature_c must be between 0 and 120")
        if not 0 < float(self.max_vram_used_ratio) <= 1:
            raise LLMResourceGovernorError("max_vram_used_ratio must be between 0 and 1")
        for workload in WORKLOAD_PRIORITY:
            limits = self.limits.get(workload)
            if limits is None or limits.max_wall_seconds <= 0 or limits.max_output_tokens <= 0:
                raise LLMResourceGovernorError(f"limits missing or invalid for {workload}")

    @classmethod
    def from_env(cls) -> "GovernorConfig":
        def integer(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except ValueError as exc:
                raise LLMResourceGovernorError(f"invalid integer setting {name}") from exc

        def number(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except ValueError as exc:
                raise LLMResourceGovernorError(f"invalid numeric setting {name}") from exc

        def boolean(name: str, default: bool) -> bool:
            value = os.getenv(name)
            if value is None:
                return default
            normalized = value.strip().casefold()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise LLMResourceGovernorError(f"invalid boolean setting {name}")

        limits = {
            "interactive": WorkloadLimits(
                max_wall_seconds=number("BHM_LLM_GOVERNOR_INTERACTIVE_WALL_SECONDS", 120.0),
                max_output_tokens=integer("BHM_LLM_GOVERNOR_INTERACTIVE_OUTPUT_TOKENS", 1_024),
            ),
            "foreground": WorkloadLimits(
                max_wall_seconds=number("BHM_LLM_GOVERNOR_FOREGROUND_WALL_SECONDS", 300.0),
                max_output_tokens=integer("BHM_LLM_GOVERNOR_FOREGROUND_OUTPUT_TOKENS", 2_048),
            ),
            "background": WorkloadLimits(
                max_wall_seconds=number("BHM_LLM_GOVERNOR_BACKGROUND_WALL_SECONDS", 900.0),
                max_output_tokens=integer("BHM_LLM_GOVERNOR_BACKGROUND_OUTPUT_TOKENS", 4_096),
            ),
        }
        return cls(
            max_concurrency=integer("BHM_LLM_GOVERNOR_MAX_CONCURRENCY", 2),
            interactive_reserve=integer("BHM_LLM_GOVERNOR_INTERACTIVE_RESERVE", 1),
            max_temperature_c=number("BHM_LLM_GOVERNOR_MAX_TEMPERATURE_C", 82.0),
            max_vram_used_ratio=number("BHM_LLM_GOVERNOR_MAX_VRAM_RATIO", 0.92),
            pause_on_user_activity=boolean("BHM_LLM_GOVERNOR_PAUSE_ON_USER_ACTIVITY", True),
            background_requires_maintenance_window=boolean(
                "BHM_LLM_GOVERNOR_BACKGROUND_REQUIRES_WINDOW", True
            ),
            require_gpu_probe=boolean("BHM_LLM_GOVERNOR_REQUIRE_GPU_PROBE", True),
            maintenance_window=MaintenanceWindow.parse(os.getenv("BHM_LLM_GOVERNOR_MAINTENANCE_WINDOW", "")),
            limits=limits,
        )


@dataclass(frozen=True)
class ResourceSnapshot:
    gpu_available: bool
    vram_used_mib: int | None = None
    vram_total_mib: int | None = None
    temperature_c: float | None = None
    observed_at: str = ""

    @property
    def vram_used_ratio(self) -> float | None:
        if not self.vram_total_mib or self.vram_total_mib <= 0 or self.vram_used_mib is None:
            return None
        return float(self.vram_used_mib) / float(self.vram_total_mib)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gpu_available": self.gpu_available,
            "vram_used_mib": self.vram_used_mib,
            "vram_total_mib": self.vram_total_mib,
            "vram_used_ratio": round(self.vram_used_ratio, 6) if self.vram_used_ratio is not None else None,
            "temperature_c": self.temperature_c,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class AdmissionRequest:
    job_id: str
    workload: LLMWorkload
    max_wall_seconds: float
    max_output_tokens: int


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    code: str
    reason: str
    job_id: str
    workload: LLMWorkload
    priority_rank: int
    max_wall_seconds: float
    max_output_tokens: int
    active_count: int
    maintenance_window_open: bool
    resource_snapshot: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "job_id": self.job_id,
            "workload": self.workload,
            "priority_rank": self.priority_rank,
            "limits": {
                "max_wall_seconds": self.max_wall_seconds,
                "max_output_tokens": self.max_output_tokens,
            },
            "active_count": self.active_count,
            "maintenance_window_open": self.maintenance_window_open,
            "resource_snapshot": self.resource_snapshot,
        }


GpuProbe = Callable[[], ResourceSnapshot]


class LLMResourceGovernor:
    def __init__(self, config: GovernorConfig, *, gpu_probe: GpuProbe | None = None) -> None:
        self.config = config
        self._gpu_probe = gpu_probe or nvidia_smi_snapshot
        self._lock = threading.RLock()
        self._active: dict[str, LLMWorkload] = {}
        self._paused = False
        self._pause_reason = ""
        self._user_activity = False

    def admit(
        self,
        request: AdmissionRequest,
        *,
        now: datetime | None = None,
        resources: ResourceSnapshot | None = None,
    ) -> AdmissionDecision:
        if request.workload not in WORKLOAD_PRIORITY:
            raise LLMResourceGovernorError(f"unsupported workload: {request.workload}")
        current = now or datetime.now(timezone.utc)
        snapshot = resources or self._gpu_probe()
        window_open = bool(self.config.maintenance_window and self.config.maintenance_window.contains(current))
        limits = self.config.limits[request.workload]
        with self._lock:
            active_count = len(self._active)
            if request.job_id in self._active:
                return self._decision(
                    request,
                    allowed=True,
                    code="already_admitted",
                    reason="job already holds an active governor reservation",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            if self._paused:
                return self._decision(
                    request,
                    allowed=False,
                    code="governor_paused",
                    reason=self._pause_reason or "resource governor is paused",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            if self.config.require_gpu_probe and not snapshot.gpu_available:
                return self._decision(
                    request,
                    allowed=False,
                    code="gpu_probe_unavailable",
                    reason="GPU/VRAM probe is unavailable; fail closed before local-LLM admission",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            if snapshot.temperature_c is not None and snapshot.temperature_c >= self.config.max_temperature_c:
                return self._decision(
                    request,
                    allowed=False,
                    code="temperature_threshold",
                    reason="GPU temperature is at or above the governor threshold",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            if snapshot.vram_used_ratio is not None and snapshot.vram_used_ratio >= self.config.max_vram_used_ratio:
                return self._decision(
                    request,
                    allowed=False,
                    code="vram_threshold",
                    reason="GPU VRAM usage is at or above the governor threshold",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            if request.max_wall_seconds > limits.max_wall_seconds:
                return self._decision(
                    request,
                    allowed=False,
                    code="wall_limit_exceeded",
                    reason="requested wall time exceeds the workload class limit",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            if request.max_output_tokens > limits.max_output_tokens:
                return self._decision(
                    request,
                    allowed=False,
                    code="output_limit_exceeded",
                    reason="requested output exceeds the workload class limit",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            if request.workload == "background" and self.config.pause_on_user_activity and self._user_activity:
                return self._decision(
                    request,
                    allowed=False,
                    code="user_activity_pause",
                    reason="background work is paused while interactive user activity is present",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            if request.workload == "background" and self.config.background_requires_maintenance_window and not window_open:
                return self._decision(
                    request,
                    allowed=False,
                    code="maintenance_window_closed",
                    reason="background work requires an explicit open maintenance window",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            noninteractive_active = sum(1 for item in self._active.values() if item != "interactive")
            noninteractive_capacity = self.config.max_concurrency - self.config.interactive_reserve
            if request.workload != "interactive" and noninteractive_active >= noninteractive_capacity:
                return self._decision(
                    request,
                    allowed=False,
                    code="interactive_reserve",
                    reason="reserved capacity is held for interactive work",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            if active_count >= self.config.max_concurrency:
                return self._decision(
                    request,
                    allowed=False,
                    code="concurrency_limit",
                    reason="governor concurrency limit reached",
                    active_count=active_count,
                    window_open=window_open,
                    snapshot=snapshot,
                    limits=limits,
                )
            self._active[request.job_id] = request.workload
            return self._decision(
                request,
                allowed=True,
                code="admitted",
                reason="resource and workload policy accepted the job",
                active_count=active_count + 1,
                window_open=window_open,
                snapshot=snapshot,
                limits=limits,
            )

    def release(self, job_id: str) -> bool:
        with self._lock:
            return self._active.pop(str(job_id), None) is not None

    def pause(self, *, reason: str = "paused by operator") -> dict[str, Any]:
        with self._lock:
            self._paused = True
            self._pause_reason = str(reason or "paused by operator")[:1_000]
            return self.status()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            self._paused = False
            self._pause_reason = ""
            return self.status()

    def set_user_activity(self, active: bool) -> dict[str, Any]:
        with self._lock:
            self._user_activity = bool(active)
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = {job_id: workload for job_id, workload in sorted(self._active.items())}
            return {
                "paused": self._paused,
                "pause_reason": self._pause_reason,
                "user_activity": self._user_activity,
                "active_count": len(active),
                "active": active,
                "max_concurrency": self.config.max_concurrency,
                "interactive_reserve": self.config.interactive_reserve,
                "maintenance_window": self.config.maintenance_window.as_text()
                if self.config.maintenance_window
                else None,
            }

    def _decision(
        self,
        request: AdmissionRequest,
        *,
        allowed: bool,
        code: str,
        reason: str,
        active_count: int,
        window_open: bool,
        snapshot: ResourceSnapshot,
        limits: WorkloadLimits,
    ) -> AdmissionDecision:
        return AdmissionDecision(
            allowed=allowed,
            code=code,
            reason=reason,
            job_id=request.job_id,
            workload=request.workload,
            priority_rank=WORKLOAD_PRIORITY[request.workload],
            max_wall_seconds=limits.max_wall_seconds,
            max_output_tokens=limits.max_output_tokens,
            active_count=active_count,
            maintenance_window_open=window_open,
            resource_snapshot=snapshot.as_dict(),
        )


def nvidia_smi_snapshot() -> ResourceSnapshot:
    """Read one bounded GPU snapshot without raising into the admission path."""

    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        first = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), "")
        used_text, total_text, temperature_text = [part.strip() for part in first.split(",", 2)]
        return ResourceSnapshot(
            gpu_available=True,
            vram_used_mib=int(float(used_text)),
            vram_total_mib=int(float(total_text)),
            temperature_c=float(temperature_text),
            observed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError):
        return ResourceSnapshot(
            gpu_available=False,
            observed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )


def _parse_minute(value: str) -> int:
    hour_text, minute_text = str(value).strip().split(":", 1)
    parsed = time(hour=int(hour_text), minute=int(minute_text))
    return parsed.hour * 60 + parsed.minute


__all__ = [
    "AdmissionDecision",
    "AdmissionRequest",
    "GovernorConfig",
    "LLMResourceGovernor",
    "LLMResourceGovernorError",
    "LLMWorkload",
    "MaintenanceWindow",
    "ResourceSnapshot",
    "WORKLOAD_PRIORITY",
    "WorkloadLimits",
    "nvidia_smi_snapshot",
]
