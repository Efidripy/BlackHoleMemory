"""Bounded, backup-first recovery of safe transient projection dead letters."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from typing import Callable

from .filesystem_boundaries import replace_bytes_safely

_LOGGER = logging.getLogger(__name__)
AUTO_REQUEUE_INTERVAL_SECONDS = 300.0
AUTO_REQUEUE_LIMIT = 2

# These are deliberately narrow.  In particular, a bare ``qdrant`` or an
# arbitrary exception is not sufficient: binding, schema and vector errors
# must remain visible as terminal data/configuration failures.
_TRANSIENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("qdrant_unavailable", re.compile(r"qdrant.{0,80}(?:unavailable|not[ _-]?ready|offline)", re.I)),
    ("connection", re.compile(r"(?:connection|connect)(?:error| refused| reset| aborted)|server disconnected|no route to host|network (?:is )?(?:unreachable|unavailable)", re.I)),
    ("timeout", re.compile(r"(?:timed? out|timeout|timeouterror|connecttimeout|readtimeout|pooltimeout)", re.I)),
    ("server_5xx", re.compile(r"(?:\b(?:status(?:[_ -]?code)?|http|code|response)[\s:=_-]*5\d\d\b|\b(?:internal server error|bad gateway|service unavailable|gateway timeout)\b)", re.I)),
)


def transient_reason(last_error: str | None) -> str | None:
    """Return a stable safe log reason for an explicitly transient error."""

    value = str(last_error or "").strip()
    if not value:
        return None
    if re.search(r"(?:^|\b)binding_[a-z0-9_.-]*", value, re.I):
        return None
    for reason, pattern in _TRANSIENT_PATTERNS:
        if pattern.search(value):
            return reason
    return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_requeue_helpers() -> Any:
    script = Path(__file__).resolve().parents[2] / "scripts" / "requeue-bhm-dead-letters.py"
    spec = importlib.util.spec_from_file_location("bhm_requeue_dead_letters_helpers", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load dead-letter backup helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _online_backup(database: Path, backup: Path) -> None:
    """Reuse the operator script's hardened SQLite online-backup helper."""

    _load_requeue_helpers()._online_backup(database, backup)


class AutoRequeueController:
    """Apply at most one five-minute, two-retry recovery pass per database."""

    def __init__(
        self,
        database: Path | str,
        *,
        runtime_dir: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.database = Path(database).expanduser().resolve()
        root = Path(runtime_dir).expanduser().resolve() if runtime_dir is not None else self.database.parents[1]
        self.state_path = root / "state" / "projection-auto-requeue.json"
        self.backup_root = root / "state" / "projection-auto-requeue-backups"
        self._clock = clock or _utc_now
        self.enabled = (os.getenv("BHM_PROJECTION_AUTO_REQUEUE", "1") != "0") if enabled is None else bool(enabled)

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"last_pass_at": None, "attempts": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("attempts", {}), dict):
            return {"last_pass_at": None, "attempts": {}}
        return payload

    def _write_state(self, state: dict[str, Any]) -> None:
        replace_bytes_safely(
            self.state_path,
            (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )

    def _interval_elapsed(self, state: dict[str, Any], now: datetime) -> bool:
        raw = state.get("last_pass_at")
        if not raw:
            return True
        try:
            previous = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return True
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        return (now - previous).total_seconds() >= AUTO_REQUEUE_INTERVAL_SECONDS

    @staticmethod
    def _qdrant_healthy(client: Any) -> bool:
        try:
            probe = getattr(client, "get_collections", None)
            if callable(probe):
                return probe() is not False
            probe = getattr(client, "health", None)
            if callable(probe):
                result = probe()
                return result is not False
        except Exception:
            return False
        return False

    def run_pass(self, qdrant_client: Any) -> list[dict[str, Any]]:
        """Probe, backup, and requeue selected transient dead letters."""

        if not self.enabled or not self.database.exists():
            return []
        now = self._clock()
        state = self._read_state()
        if not self._interval_elapsed(state, now) or not self._qdrant_healthy(qdrant_client):
            return []

        connection = sqlite3.connect(self.database)
        try:
            rows = connection.execute(
                "SELECT event_id, attempts, last_error FROM memory_outbox "
                "WHERE status = 'dead_letter' ORDER BY event_id"
            ).fetchall()
        finally:
            connection.close()
        counts = state.setdefault("attempts", {})
        selected: list[tuple[str, int, str]] = []
        for event_id, _attempts, last_error in rows:
            event_key = str(event_id)
            count = int(counts.get(event_key, 0) or 0)
            reason = transient_reason(last_error)
            if reason is not None and count < AUTO_REQUEUE_LIMIT:
                selected.append((event_key, count + 1, reason))

        # Record the pass even when no row qualifies, so a busy dead-letter
        # queue cannot cause repeated probes more often than once per 5 minutes.
        state["last_pass_at"] = _iso(now)
        if not selected:
            self._write_state(state)
            return []

        digest = hashlib.sha256(
            "\n".join(item[0] for item in selected).encode("utf-8")
        ).hexdigest()[:16]
        backup = self.backup_root / f"memories-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{digest}.sqlite3"
        _online_backup(self.database, backup)

        placeholders = ",".join("?" for _ in selected)
        updated_at = _iso(now)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE memory_outbox SET status = 'pending', attempts = 0, "
                f"available_at = ?, claimed_at = NULL, claim_token = NULL, updated_at = ? "
                f"WHERE status = 'dead_letter' AND event_id IN ({placeholders})",
                (updated_at, updated_at, *(item[0] for item in selected)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        records: list[dict[str, Any]] = []
        for event_id, attempt_number, reason in selected:
            counts[event_id] = attempt_number
            record = {"event_id": event_id, "reason": reason, "attempt": attempt_number}
            records.append(record)
            _LOGGER.info(
                "projection_auto_requeue event_id=%s reason=%s attempt=%d",
                event_id,
                reason,
                attempt_number,
            )
        self._write_state(state)
        return records


def run_auto_requeue_pass(database: Path | str, qdrant_client: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Convenience API used by the worker and unit tests."""

    return AutoRequeueController(database, **kwargs).run_pass(qdrant_client)


__all__ = ["AutoRequeueController", "run_auto_requeue_pass", "transient_reason"]
