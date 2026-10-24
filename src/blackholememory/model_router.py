"""Capability-based local model routing with measured context profiles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


MODEL_ROUTER_SCHEMA_VERSION = "bhm.llm.model-router.v1"
MODEL_ROUTER_CONTEXT_PROFILES = (8192, 16384, 32768)
MODEL_ROUTER_CAPABILITIES = ("classification", "coding", "reasoning", "vision", "embedding", "json")
# Prefer the least expensive local model that satisfies the requested
# capability/context contract.  Escalation remains fail-closed when the
# lighter tier cannot satisfy the evidence or risk boundary.
MODEL_SELECTION_POLICY = {
    "strategy": "minimum_sufficient_local_tier",
    "tier_order": ["light", "standard", "deep"],
    "cloud_fallback": False,
    "autonomous_apply": False,
}
DEFAULT_CONTEXT_MEASUREMENTS = (
    {"context_tokens": 8192, "ok": True, "latency_ms": 788.0, "tokens_per_second": 0.0, "source": "P17.1-local-smoke"},
)


class ModelRouterError(ValueError):
    """Raised when a routing request cannot be satisfied fail-closed."""


DEFAULT_MODEL_INVENTORY = (
    {
        "model_id": "qwen2.5-coder-7b-instruct",
        "capabilities": ("classification", "coding", "reasoning", "json"),
        "context_window": 131_072,
        "local_only": True,
        "available": True,
        "latency_ms": 788.0,
        "selection_tier": 1,
    },
    {
        "model_id": "vision-model-unconfirmed",
        "capabilities": ("vision", "reasoning", "json"),
        "context_window": 32_768,
        "local_only": True,
        "available": False,
        "latency_ms": 1500.0,
        "selection_tier": 3,
    },
    {
        "model_id": "text-embedding-nomic-embed-text-v1.5",
        "capabilities": ("embedding",),
        "context_window": 2048,
        "local_only": True,
        "available": True,
        "latency_ms": 100.0,
        "selection_tier": 1,
    },
)


@dataclass(frozen=True)
class RouteDecision:
    status: str
    task_type: str
    required_capabilities: tuple[str, ...]
    model_id: str | None
    profile_tokens: int | None
    reason_codes: tuple[str, ...]
    local_only: bool = True
    execution_enabled: bool = False
    auto_apply: bool = False
    selection_tier: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MODEL_ROUTER_SCHEMA_VERSION,
            "status": self.status,
            "task_type": self.task_type,
            "required_capabilities": list(self.required_capabilities),
            "model_id": self.model_id,
            "profile_tokens": self.profile_tokens,
            "reason_codes": list(self.reason_codes),
            "local_only": self.local_only,
            "execution_enabled": self.execution_enabled,
            "auto_apply": self.auto_apply,
            "selection_tier": self.selection_tier,
            "authority": "proposal",
        }


def build_context_profiles(measurements: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return explicit 8k/16k/32k measurement status without inventing results."""

    source_measurements = DEFAULT_CONTEXT_MEASUREMENTS if measurements is None else measurements
    measured = {int(item.get("context_tokens")): dict(item) for item in source_measurements if str(item.get("context_tokens") or "").isdigit()}
    profiles: list[dict[str, Any]] = []
    for tokens in MODEL_ROUTER_CONTEXT_PROFILES:
        item = measured.get(tokens, {})
        ok = bool(item.get("ok"))
        profiles.append(
            {
                "profile_tokens": tokens,
                "status": "measured" if ok else "not_measured",
                "ok": ok,
                "latency_ms": _number(item.get("latency_ms")),
                "tokens_per_second": _number(item.get("tokens_per_second")),
                "measurement_digest": _sha256(_canonical_json(item)) if item else None,
                "requires_benchmark": not ok,
            }
        )
    return profiles


def route_model(
    task_type: str,
    *,
    required_capabilities: Sequence[str] = (),
    context_tokens: int = 8192,
    measurements: Sequence[Mapping[str, Any]] | None = None,
    models: Sequence[Mapping[str, Any]] | None = None,
) -> RouteDecision:
    """Choose a local model only when capability and measured profile gates pass."""

    task = str(task_type or "").strip().casefold().replace(" ", "_")
    if not task:
        raise ModelRouterError("task_type is required")
    requested = tuple(dict.fromkeys(str(item or "").strip().casefold() for item in required_capabilities if str(item or "").strip()))
    unknown = sorted(set(requested) - set(MODEL_ROUTER_CAPABILITIES))
    if unknown:
        raise ModelRouterError(f"unsupported capabilities: {', '.join(unknown)}")
    try:
        context = int(context_tokens)
    except (TypeError, ValueError):
        raise ModelRouterError("context_tokens must be an integer") from None
    if context < 1:
        raise ModelRouterError("context_tokens must be positive")
    profiles = build_context_profiles(measurements)
    profile = next((item for item in profiles if context <= int(item["profile_tokens"])), None)
    reasons: list[str] = []
    if profile is None:
        return RouteDecision("rejected", task, requested, None, None, ("context_profile_unsupported",))
    if profile["status"] != "measured":
        return RouteDecision("rejected", task, requested, None, int(profile["profile_tokens"]), ("context_profile_not_measured",))
    inventory = [_normalize_model(item) for item in (models or DEFAULT_MODEL_INVENTORY)]
    candidates = [
        item
        for item in inventory
        if item["local_only"] and item["available"] and set(requested).issubset(set(item["capabilities"])) and int(item["context_window"]) >= context
    ]
    if not candidates:
        if requested and "vision" in requested:
            reasons.append("vision_capability_unconfirmed")
        else:
            reasons.append("local_capability_missing")
        return RouteDecision("rejected", task, requested, None, int(profile["profile_tokens"]), tuple(reasons))
    chosen = sorted(
        candidates,
        key=lambda item: (int(item["selection_tier"]), float(item["latency_ms"]), item["model_id"]),
    )[0]
    reasons.extend(
        (
            "capability_match",
            "measured_context_profile",
            "local_only_attestation",
            "minimum_sufficient_tier",
        )
    )
    return RouteDecision(
        "routed",
        task,
        requested,
        str(chosen["model_id"]),
        int(profile["profile_tokens"]),
        tuple(reasons),
        selection_tier=int(chosen["selection_tier"]),
    )


def router_snapshot(
    *,
    measurements: Sequence[Mapping[str, Any]] | None = None,
    models: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Expose the routing contract and its evidence status."""

    inventory = [_normalize_model(item) for item in (models or DEFAULT_MODEL_INVENTORY)]
    return {
        "schema_version": MODEL_ROUTER_SCHEMA_VERSION,
        "capabilities": list(MODEL_ROUTER_CAPABILITIES),
        "context_profiles": build_context_profiles(measurements),
        "models": inventory,
        "local_only_required": True,
        "selection_policy": dict(MODEL_SELECTION_POLICY),
        "cloud_fallback": False,
        "execution_enabled": False,
        "auto_apply": False,
    }


def _normalize_model(raw: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(raw)
    capabilities = sorted({str(value).strip().casefold() for value in (item.get("capabilities") or []) if str(value).strip()})
    return {
        "model_id": str(item.get("model_id") or "unknown")[:160],
        "capabilities": capabilities,
        "context_window": max(int(item.get("context_window") or 0), 0),
        "local_only": bool(item.get("local_only", True)),
        "available": bool(item.get("available", False)),
        "latency_ms": round(_number(item.get("latency_ms")), 3),
        "selection_tier": min(max(int(item.get("selection_tier") or 2), 1), 3),
    }


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if number != number else number


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_MODEL_INVENTORY",
    "DEFAULT_CONTEXT_MEASUREMENTS",
    "MODEL_ROUTER_CAPABILITIES",
    "MODEL_ROUTER_CONTEXT_PROFILES",
    "MODEL_ROUTER_SCHEMA_VERSION",
    "MODEL_SELECTION_POLICY",
    "ModelRouterError",
    "build_context_profiles",
    "route_model",
    "router_snapshot",
]
