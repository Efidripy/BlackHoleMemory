"""Explicit storage backend mode and readiness state machine."""

from __future__ import annotations

import os
from dataclasses import asdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


STORAGE_MODE_ENV = "BHM_STORAGE_MODE"


class StorageMode(StrEnum):
    """Configured policy for the Qdrant-backed vector contour."""

    REMOTE_REQUIRED = "remote-required"
    REMOTE_PREFERRED = "remote-preferred"
    EMBEDDED_LOCAL = "embedded-local"


class StorageReadiness(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    NOT_READY = "not-ready"


@dataclass(frozen=True)
class StorageState:
    configured_mode: str
    remote_available: bool
    backend: str
    readiness: str
    reason: str

    @property
    def ready(self) -> bool:
        return self.readiness == StorageReadiness.READY.value

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"ready": self.ready}


_MODE_ALIASES = {
    "remote-required": StorageMode.REMOTE_REQUIRED,
    "remote_required": StorageMode.REMOTE_REQUIRED,
    "remote": StorageMode.REMOTE_REQUIRED,
    "remote-preferred": StorageMode.REMOTE_PREFERRED,
    "remote_preferred": StorageMode.REMOTE_PREFERRED,
    "preferred": StorageMode.REMOTE_PREFERRED,
    "embedded-local": StorageMode.EMBEDDED_LOCAL,
    "embedded_local": StorageMode.EMBEDDED_LOCAL,
    "local": StorageMode.EMBEDDED_LOCAL,
}


def resolve_storage_mode(value: str | StorageMode | None = None) -> StorageMode:
    """Resolve storage policy; invalid values fail closed to remote-required."""

    if isinstance(value, StorageMode):
        return value
    raw_value = os.getenv(STORAGE_MODE_ENV, StorageMode.REMOTE_REQUIRED.value) if value is None else value
    return _MODE_ALIASES.get(str(raw_value).strip().lower(), StorageMode.REMOTE_REQUIRED)


def evaluate_storage_state(
    mode: str | StorageMode | None,
    *,
    remote_available: bool,
) -> StorageState:
    """Evaluate effective backend and readiness without performing I/O."""

    configured = resolve_storage_mode(mode)
    if configured is StorageMode.REMOTE_REQUIRED:
        if remote_available:
            return StorageState(configured.value, True, "remote", StorageReadiness.READY.value, "remote_qdrant_ready")
        return StorageState(
            configured.value,
            False,
            "unavailable",
            StorageReadiness.NOT_READY.value,
            "remote_qdrant_required_but_unavailable",
        )
    if configured is StorageMode.REMOTE_PREFERRED:
        if remote_available:
            return StorageState(configured.value, True, "remote", StorageReadiness.READY.value, "remote_qdrant_ready")
        return StorageState(
            configured.value,
            False,
            "embedded-local",
            StorageReadiness.DEGRADED.value,
            "explicit_remote_preferred_fallback",
        )
    return StorageState(
        configured.value,
        bool(remote_available),
        "embedded-local",
        StorageReadiness.DEGRADED.value,
        "explicit_embedded_local_mode",
    )

