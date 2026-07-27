"""Bounded launcher start/readiness/rollback primitives."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


Probe = Callable[[], tuple[bool, str]]
Start = Callable[[], Any]
Rollback = Callable[[Any], None]


@dataclass(frozen=True)
class ReadinessResult:
    ok: bool
    started: bool
    rolled_back: bool
    attempts: int
    elapsed_ms: float
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "started": self.started,
            "rolled_back": self.rolled_back,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "detail": self.detail,
        }


def probe_http(url: str, *, timeout: float = 2.0, require_json_ok: bool = False) -> tuple[bool, str]:
    """Probe a health URL without treating a transient network error as fatal."""

    try:
        request = urllib.request.Request(url, headers={"User-Agent": "BHM-Control-Deck"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(response.status) != 200:
                return False, f"HTTP {response.status}"
            if not require_json_ok:
                return True, "HTTP 200"
            payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and bool(payload.get("ok")):
                return True, "ready"
            return False, "health payload is not ready"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        detail = str(exc).replace("\r", " ").replace("\n", " ").strip()
        return False, detail[:140] or exc.__class__.__name__


def wait_for_readiness(
    probe: Probe,
    *,
    timeout_seconds: float = 45.0,
    poll_seconds: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ReadinessResult:
    """Poll a bounded readiness probe until success or timeout."""

    timeout = max(float(timeout_seconds), 0.0)
    poll = max(float(poll_seconds), 0.01)
    started_at = monotonic()
    attempts = 0
    last_detail = "not probed"
    while True:
        attempts += 1
        try:
            ok, detail = probe()
        except Exception as exc:  # pragma: no cover - defensive boundary
            ok, detail = False, str(exc)
        last_detail = str(detail or "unknown readiness state")[:240]
        if ok:
            return ReadinessResult(
                ok=True,
                started=True,
                rolled_back=False,
                attempts=attempts,
                elapsed_ms=round(max(monotonic() - started_at, 0.0) * 1000, 3),
                detail=last_detail,
            )
        elapsed = max(monotonic() - started_at, 0.0)
        if elapsed >= timeout:
            return ReadinessResult(
                ok=False,
                started=True,
                rolled_back=False,
                attempts=attempts,
                elapsed_ms=round(elapsed * 1000, 3),
                detail=last_detail,
            )
        sleep(min(poll, max(timeout - elapsed, 0.0)))


def start_when_ready(
    start: Start,
    probe: Probe,
    *,
    rollback: Rollback | None = None,
    timeout_seconds: float = 45.0,
    poll_seconds: float = 1.0,
) -> ReadinessResult:
    """Start only when needed, wait for readiness, and rollback on timeout."""

    try:
        already_ready, detail = probe()
    except Exception as exc:  # pragma: no cover - defensive boundary
        already_ready, detail = False, str(exc)
    if already_ready:
        return ReadinessResult(
            ok=True,
            started=False,
            rolled_back=False,
            attempts=1,
            elapsed_ms=0.0,
            detail=str(detail or "already ready")[:240],
        )

    token = start()
    result = wait_for_readiness(
        probe,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )
    if result.ok or rollback is None:
        return result
    rolled_back = False
    try:
        rollback(token)
        rolled_back = True
    except Exception as exc:  # pragma: no cover - defensive boundary
        result = ReadinessResult(
            ok=False,
            started=True,
            rolled_back=False,
            attempts=result.attempts,
            elapsed_ms=result.elapsed_ms,
            detail=f"{result.detail}; rollback failed: {exc}",
        )
    if rolled_back:
        result = ReadinessResult(
            ok=False,
            started=True,
            rolled_back=True,
            attempts=result.attempts,
            elapsed_ms=result.elapsed_ms,
            detail=result.detail,
        )
    return result


__all__ = ["ReadinessResult", "probe_http", "start_when_ready", "wait_for_readiness"]
