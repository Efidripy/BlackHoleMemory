"""Bounded readiness contract for the native MCP client wrapper.

The wrapper has three independent gates: the BHM API must be ready, the
broker pipe must accept a connection, and the MCP protocol must complete
initialize/catalog negotiation.  This module owns the first gate's bounded
deadline and the cross-process single-flight lock used by explicit auto-start.
It deliberately does not start a service by itself; callers provide the
canonical launcher callback only when an operator explicitly opted in.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Callable


SCHEMA_VERSION = "bhm.mcp.readiness.v1"
MAX_DETAIL_LENGTH = 240
MAX_HISTORY = 32

_STAGES = ("api", "broker", "protocol", "catalog")
_STATES = {
    "api": {"unknown", "probing", "starting", "ready", "unavailable", "failed", "timeout"},
    "broker": {"unknown", "connecting", "connected", "failed", "timeout"},
    "protocol": {"not_started", "initializing", "ready", "failed"},
    "catalog": {"unknown", "fetching", "ready", "stale", "failed"},
}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_detail(value: object) -> str:
    detail = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    return detail[:MAX_DETAIL_LENGTH]


class ReadinessError(RuntimeError):
    """A bounded readiness decision that must remain visible to the client."""

    def __init__(self, stage: str, code: str, detail: str) -> None:
        self.stage = stage
        self.code = code
        self.detail = _clean_detail(detail) or code
        super().__init__(f"{stage}:{code}: {self.detail}")


@dataclass(frozen=True)
class ReadinessConfig:
    deadline_seconds: float = 15.0
    api_probe_timeout_seconds: float = 3.0
    poll_seconds: float = 0.25
    auto_start: bool = False
    lock_path: Path | None = None
    max_history: int = MAX_HISTORY

    def __post_init__(self) -> None:
        if not 0.25 <= float(self.deadline_seconds) <= 120.0:
            raise ValueError("readiness deadline must be between 0.25 and 120 seconds")
        if not 0.1 <= float(self.api_probe_timeout_seconds) <= 5.0:
            raise ValueError("API probe timeout must be between 0.1 and 5 seconds")
        if not 0.025 <= float(self.poll_seconds) <= 2.0:
            raise ValueError("readiness poll interval must be between 0.025 and 2 seconds")
        if not 4 <= int(self.max_history) <= MAX_HISTORY:
            raise ValueError(f"max_history must be between 4 and {MAX_HISTORY}")


class ReadinessSingleFlightLock:
    """A bounded local-plus-cross-process lock for canonical auto-start.

    The in-process lock prevents duplicate launches from threads in one
    wrapper process.  The one-byte file lock covers separate Python/PowerShell
    wrapper processes without relying on stale PID files.
    """

    _registry_guard = threading.Lock()
    _local_locks: dict[str, threading.Lock] = {}

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._local_lock: threading.Lock | None = None
        self._handle = None

    @classmethod
    def _get_local_lock(cls, key: str) -> threading.Lock:
        with cls._registry_guard:
            return cls._local_locks.setdefault(key, threading.Lock())

    def acquire(self, timeout_seconds: float, *, sleeper: Callable[[float], None] = time.sleep) -> bool:
        timeout = max(float(timeout_seconds), 0.0)
        key = os.path.normcase(os.path.abspath(str(self.path)))
        local_lock = self._get_local_lock(key)
        if not local_lock.acquire(timeout=timeout):
            return False
        self._local_lock = local_lock
        deadline = time.monotonic() + timeout
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            self._handle = handle
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except (BlockingIOError, OSError):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.release()
                        return False
                    sleeper(min(0.05, remaining))
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
            try:
                handle.close()
            except OSError:
                pass
        local_lock, self._local_lock = self._local_lock, None
        if local_lock is not None and local_lock.locked():
            local_lock.release()

    def __enter__(self) -> "ReadinessSingleFlightLock":
        if not self.acquire(0.0):
            raise TimeoutError("readiness lock was not acquired")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()


def default_lock_path(repo_root: Path) -> Path:
    configured = os.getenv("BHM_MCP_STARTUP_LOCK_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "BlackHoleMemory" / "mcp-readiness.lock"
    # `.runtime` is the canonical disposable runtime boundary. Do not fall
    # back to a public `runtime/` tree when LOCALAPPDATA is unavailable.
    return Path(repo_root) / ".runtime" / "mcp" / "mcp-readiness.lock"


@dataclass(frozen=True)
class ReadinessEvent:
    sequence: int
    stage: str
    state: str
    detail: str
    at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "stage": self.stage,
            "state": self.state,
            "detail": self.detail,
            "at": self.at,
        }


class ReadinessCoordinator:
    """Track readiness stages and execute one bounded API start transaction."""

    def __init__(
        self,
        *,
        config: ReadinessConfig | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], str] = _utc_timestamp,
    ) -> None:
        self.config = config or ReadinessConfig()
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._clock = clock
        self._deadline = monotonic() + self.config.deadline_seconds
        self._lock = threading.RLock()
        self._stages = {"api": "unknown", "broker": "unknown", "protocol": "not_started", "catalog": "unknown"}
        self._errors: dict[str, str] = {}
        self._events: list[ReadinessEvent] = []
        self._sequence = 0
        self._auto_start_attempted = False

    @property
    def auto_start_attempted(self) -> bool:
        with self._lock:
            return self._auto_start_attempted

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._monotonic())

    def mark(self, stage: str, state: str, detail: str = "") -> None:
        if stage not in _STAGES:
            raise ValueError(f"unknown readiness stage: {stage}")
        if state not in _STATES[stage]:
            raise ValueError(f"invalid readiness state {stage}={state}")
        clean_detail = _clean_detail(detail)
        with self._lock:
            self._stages[stage] = state
            if clean_detail and state in {"failed", "unavailable", "timeout", "stale"}:
                self._errors[stage] = clean_detail
            elif state in {"ready", "connected"}:
                self._errors.pop(stage, None)
            self._sequence += 1
            self._events.append(ReadinessEvent(self._sequence, stage, state, clean_detail, self._clock()))
            if len(self._events) > self.config.max_history:
                del self._events[: len(self._events) - self.config.max_history]

    def stage(self, stage: str) -> str:
        with self._lock:
            return self._stages[stage]

    def _probe_once(self, probe: Callable[[float], tuple[bool, str]]) -> tuple[bool, str]:
        timeout = min(self.config.api_probe_timeout_seconds, self.remaining_seconds())
        if timeout <= 0:
            return False, "readiness deadline exhausted"
        try:
            ready, detail = probe(timeout)
            return bool(ready), _clean_detail(detail)
        except Exception as exc:  # pragma: no cover - defensive boundary
            return False, _clean_detail(f"{type(exc).__name__}: {exc}")

    def _poll_until_ready(self, probe: Callable[[float], tuple[bool, str]]) -> bool:
        while self.remaining_seconds() > 0:
            self.mark("api", "probing", "waiting for API readiness")
            ready, detail = self._probe_once(probe)
            if ready:
                self.mark("api", "ready", detail or "API readiness confirmed")
                return True
            self.mark("api", "unavailable", detail or "API is not ready")
            self._sleeper(min(self.config.poll_seconds, self.remaining_seconds()))
        self.mark("api", "timeout", "API readiness deadline exceeded")
        return False

    def ensure_api_ready(
        self,
        probe: Callable[[float], tuple[bool, str]],
        *,
        launcher: Callable[[float], tuple[bool, str]] | None = None,
        lock_path: Path | None = None,
    ) -> bool:
        """Require API readiness, optionally invoking one canonical launcher.

        The lock is held from the re-probe through the launcher and readiness
        poll.  A waiting peer therefore re-probes instead of spawning a second
        startup while the first process is still warming.
        """

        if self.stage("api") == "ready":
            return True
        self.mark("api", "probing", "checking BHM API readiness")
        ready, detail = self._probe_once(probe)
        if ready:
            self.mark("api", "ready", detail or "API readiness confirmed")
            return True
        self.mark("api", "unavailable", detail or "API is not ready")
        if not self.config.auto_start:
            raise ReadinessError("api", "api_unavailable", detail or "BHM API is not ready; connect-only mode is active")
        if launcher is None:
            raise ReadinessError("api", "auto_start_launcher_missing", "explicit auto-start has no canonical launcher")
        if self.remaining_seconds() <= 0:
            self.mark("api", "timeout", "API readiness deadline exceeded")
            raise ReadinessError("api", "readiness_deadline_exceeded", "API readiness deadline exceeded")
        lock = ReadinessSingleFlightLock(lock_path or self.config.lock_path or Path("mcp-readiness.lock"))
        if not lock.acquire(self.remaining_seconds(), sleeper=self._sleeper):
            self.mark("api", "timeout", "single-flight startup lock deadline exceeded")
            raise ReadinessError("api", "startup_lock_timeout", "single-flight startup lock deadline exceeded")
        try:
            ready, detail = self._probe_once(probe)
            if ready:
                self.mark("api", "ready", detail or "API became ready while waiting for launcher")
                return True
            if not self._auto_start_attempted:
                self._auto_start_attempted = True
                self.mark("api", "starting", "invoking canonical launcher after explicit opt-in")
                try:
                    started, start_detail = launcher(self.remaining_seconds())
                except Exception as exc:  # pragma: no cover - defensive boundary
                    started, start_detail = False, f"{type(exc).__name__}: {exc}"
                if not started:
                    self.mark("api", "failed", start_detail or "canonical launcher failed")
                    raise ReadinessError("api", "auto_start_failed", start_detail or "canonical launcher failed")
            if self._poll_until_ready(probe):
                return True
            raise ReadinessError("api", "readiness_deadline_exceeded", "API readiness deadline exceeded after canonical launcher")
        finally:
            lock.release()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            api_ready = self._stages["api"] == "ready"
            broker_ready = self._stages["broker"] == "connected"
            protocol_ready = self._stages["protocol"] == "ready"
            catalog_ready = self._stages["catalog"] == "ready"
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": api_ready and broker_ready and protocol_ready and catalog_ready,
                "api": self._stages["api"],
                "broker": self._stages["broker"],
                "protocol": self._stages["protocol"],
                "catalog": self._stages["catalog"],
                "deadline": {
                    "configured_seconds": self.config.deadline_seconds,
                    "remaining_seconds": round(self.remaining_seconds(), 6),
                    "expired": self.remaining_seconds() <= 0,
                },
                "auto_start": {
                    "requested": self.config.auto_start,
                    "attempted": self._auto_start_attempted,
                    "launcher": "canonical:start-bhm-authoritative.ps1" if self.config.auto_start else "disabled",
                },
                "errors": dict(self._errors),
                "events": [event.as_dict() for event in self._events],
            }


__all__ = [
    "ReadinessConfig",
    "ReadinessCoordinator",
    "ReadinessError",
    "ReadinessEvent",
    "ReadinessSingleFlightLock",
    "SCHEMA_VERSION",
    "default_lock_path",
]
