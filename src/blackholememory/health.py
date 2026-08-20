from __future__ import annotations

from dataclasses import asdict, dataclass

from .mem0_adapter import _remote_qdrant_available
from .storage_state import qdrant_required_for_core


@dataclass
class DependencyStatus:
    name: str
    ok: bool
    detail: str
    required: bool = True


def check_qdrant() -> DependencyStatus:
    try:
        if _remote_qdrant_available():
            return DependencyStatus("qdrant", True, "ok")
        return DependencyStatus("qdrant", False, "qdrant_unavailable")
    except Exception:  # pragma: no cover - runtime probe
        # Dependency exceptions may contain hostnames, paths or credentials;
        # expose only the stable health-contract code to callers.
        return DependencyStatus("qdrant", False, "qdrant_unavailable")


def dependency_report(include_optional: bool = False, *, require_qdrant: bool | None = None) -> dict:
    qdrant = check_qdrant()
    qdrant.required = qdrant_required_for_core() if require_qdrant is None else bool(require_qdrant)
    checks = [qdrant]
    if not include_optional:
        checks = [item for item in checks if item.required]
    required_checks = [item for item in checks if item.required]
    return {
        "ok": all(item.ok for item in required_checks),
        "required_ok": all(item.ok for item in required_checks),
        "optional_ok": all(item.ok for item in checks if not item.required) if any(not item.required for item in checks) else True,
        "dependencies": [asdict(item) for item in checks],
    }
