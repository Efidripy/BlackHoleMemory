"""BHM-native bounded context profile resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONTEXT_PROFILE = "standard"
CONTEXT_PROFILE_CONFIG = "config/context-profiles.json"


@dataclass(frozen=True)
class ContextProfile:
    name: str
    token_budget: int
    limit: int
    max_item_chars: int
    include_archived: bool = False
    include_logs: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "token_budget": self.token_budget,
            "limit": self.limit,
            "max_item_chars": self.max_item_chars,
            "include_archived": self.include_archived,
            "include_logs": self.include_logs,
        }


def _config_path(repo_root: Path | None = None) -> Path:
    root = repo_root or Path(__file__).resolve().parents[2]
    return root / CONTEXT_PROFILE_CONFIG


def load_context_profiles(repo_root: Path | None = None) -> tuple[str, dict[str, ContextProfile]]:
    path = _config_path(repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid context profile config: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        raise ValueError("context profile config must contain an object-valued profiles map")
    profiles: dict[str, ContextProfile] = {}
    for raw_name, raw_profile in payload["profiles"].items():
        if not isinstance(raw_profile, dict):
            raise ValueError(f"context profile must be an object: {raw_name!r}")
        name = str(raw_name).strip().casefold()
        profiles[name] = ContextProfile(
            name=name,
            token_budget=_bounded_int(raw_profile.get("token_budget"), minimum=64, maximum=8000, field="token_budget"),
            limit=_bounded_int(raw_profile.get("limit"), minimum=1, maximum=50, field="limit"),
            max_item_chars=_bounded_int(
                raw_profile.get("max_item_chars"),
                minimum=80,
                maximum=1600,
                field="max_item_chars",
            ),
            include_archived=bool(raw_profile.get("include_archived", False)),
            include_logs=bool(raw_profile.get("include_logs", False)),
        )
    default = str(payload.get("default_profile") or DEFAULT_CONTEXT_PROFILE).strip().casefold()
    if default not in profiles:
        raise ValueError(f"default context profile is not defined: {default!r}")
    return default, profiles


def resolve_context_profile(name: str | None = None, *, repo_root: Path | None = None) -> ContextProfile:
    default, profiles = load_context_profiles(repo_root)
    requested = str(name or default).strip().casefold() or default
    aliases = {
        "low": "low-context",
        "low_context": "low-context",
        "standard-context": "standard",
        "standard_context": "standard",
        "deep-context": "deep",
        "deep_context": "deep",
    }
    canonical = aliases.get(requested, requested)
    try:
        return profiles[canonical]
    except KeyError as exc:
        raise ValueError(f"unknown context profile: {name!r}") from exc


def _bounded_int(value: Any, *, minimum: int, maximum: int, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"context profile {field} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"context profile {field} must be between {minimum} and {maximum}")
    return parsed


__all__ = [
    "CONTEXT_PROFILE_CONFIG",
    "DEFAULT_CONTEXT_PROFILE",
    "ContextProfile",
    "load_context_profiles",
    "resolve_context_profile",
]
