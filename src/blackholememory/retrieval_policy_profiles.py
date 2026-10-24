"""Default-off, deterministic retrieval policy previews.

Profiles operate only on already-produced bounded numeric contributions. They
cannot call providers, read projections, mutate SQLite, or activate runtime
ranking. A later operator surface may consume its preview receipt, but it must
not treat this module as activation authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "bhm.retrieval-policy-profile.v1"
_CHANNELS = ("lexical", "vector", "entity", "recency", "provenance")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_UNSAFE_IDENTIFIER = re.compile(r"(?:bearer|password|secret|token|authorization|akia[0-9a-z]{16})", re.I)


class RetrievalPolicyError(ValueError):
    """Raised when a profile, preview, or canary receipt is unsafe."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _id(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(text) or _UNSAFE_IDENTIFIER.search(text):
        raise ValueError(f"{field_name} is invalid")
    return text


class RetrievalPolicyProfile(BaseModel):
    """One content-free, versioned profile; all profiles are default-off."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=40)
    weights: dict[str, float]
    max_candidates: int = Field(default=50, ge=1, le=200)
    default_enabled: bool = False

    @field_validator("profile_id", "version")
    @classmethod
    def _identifiers(cls, value: str, info: Any) -> str:
        return _id(value, f"profile.{info.field_name}")

    @field_validator("weights")
    @classmethod
    def _weights(cls, value: Mapping[str, Any]) -> dict[str, float]:
        if set(value) != set(_CHANNELS):
            raise ValueError("profile weights must cover exactly the supported channels")
        normalized = {name: round(float(value[name]), 6) for name in _CHANNELS}
        if any(not math.isfinite(weight) or weight < 0 or weight > 1 for weight in normalized.values()) or sum(normalized.values()) <= 0:
            raise ValueError("profile weights are outside bounds")
        return normalized

    @model_validator(mode="after")
    def _default_off(self) -> "RetrievalPolicyProfile":
        if self.default_enabled:
            raise ValueError("retrieval policy profiles are default-off")
        return self

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class RetrievalPolicyCandidate(BaseModel):
    """Allowlisted score-only input; it deliberately has no query/content/path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1, max_length=160)
    project: str = Field(min_length=1, max_length=160)
    scores: dict[str, float]

    @field_validator("candidate_id", "project")
    @classmethod
    def _identifiers(cls, value: str, info: Any) -> str:
        return _id(value, f"candidate.{info.field_name}")

    @field_validator("scores")
    @classmethod
    def _scores(cls, value: Mapping[str, Any]) -> dict[str, float]:
        if set(value) != set(_CHANNELS):
            raise ValueError("candidate scores must cover exactly the supported channels")
        normalized = {name: round(float(value[name]), 6) for name in _CHANNELS}
        if any(not math.isfinite(score) or score < 0 or score > 1 for score in normalized.values()):
            raise ValueError("candidate scores are outside bounds")
        return normalized


def build_retrieval_policy_preview(
    profile: RetrievalPolicyProfile,
    candidates: Sequence[RetrievalPolicyCandidate | Mapping[str, Any]],
    *,
    project: str,
) -> dict[str, Any]:
    """Score a bounded fixture deterministically; this is never a live query."""

    target_project = _id(project, "preview.project")
    if len(candidates) > profile.max_candidates:
        raise RetrievalPolicyError("candidate count exceeds profile limit")
    parsed = tuple(
        item if isinstance(item, RetrievalPolicyCandidate) else RetrievalPolicyCandidate.model_validate(item)
        for item in candidates
    )
    if len({item.candidate_id for item in parsed}) != len(parsed):
        raise RetrievalPolicyError("candidate ids must be unique")
    if any(item.project != target_project for item in parsed):
        raise RetrievalPolicyError("candidate project mismatch")
    ranked = []
    for item in parsed:
        contributions = {channel: round(item.scores[channel] * profile.weights[channel], 6) for channel in _CHANNELS}
        ranked.append({"candidate_id": item.candidate_id, "contributions": contributions, "score": round(sum(contributions.values()), 6)})
    ranked.sort(key=lambda item: (-item["score"], item["candidate_id"]))
    core = {"schema_version": SCHEMA_VERSION, "project": target_project, "profile": profile.model_dump(mode="json"), "profile_digest": profile.digest(), "ranked": ranked}
    return {**core, "preview_digest": _digest(core), "execution": {"read_only": True, "default_enabled": False, "live_retrieval_changed": False, "sqlite_mutation": False, "mem0_mutation": False, "qdrant_mutation": False, "graph_mutation": False}}


def build_policy_canary_receipt(
    preview: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    p95_latency_ms: float,
    max_p95_latency_ms: float,
) -> dict[str, Any]:
    """Compare two previews; a passing receipt still cannot activate a policy."""

    if not verify_retrieval_policy_preview(preview) or not verify_retrieval_policy_preview(baseline):
        raise RetrievalPolicyError("canary requires valid preview digests")
    if preview.get("project") != baseline.get("project"):
        raise RetrievalPolicyError("canary previews must share project")
    if not math.isfinite(p95_latency_ms) or not math.isfinite(max_p95_latency_ms) or max_p95_latency_ms <= 0 or p95_latency_ms < 0:
        raise RetrievalPolicyError("canary latency bounds are invalid")
    candidate_ids = [item["candidate_id"] for item in preview.get("ranked", [])]
    baseline_ids = [item["candidate_id"] for item in baseline.get("ranked", [])]
    core = {"schema_version": "bhm.retrieval-policy-canary.v1", "project": preview["project"], "profile_preview_digest": preview["preview_digest"], "baseline_preview_digest": baseline["preview_digest"], "rank_changed": candidate_ids != baseline_ids, "p95_latency_ms": round(float(p95_latency_ms), 3), "max_p95_latency_ms": round(float(max_p95_latency_ms), 3), "canary_passed": p95_latency_ms <= max_p95_latency_ms}
    return {**core, "receipt_digest": _digest(core), "execution": {"canary_only": True, "activation_performed": False, "rollback": "retain-current-policy"}}


def verify_retrieval_policy_preview(preview: Mapping[str, Any]) -> bool:
    if str(preview.get("schema_version") or "") != SCHEMA_VERSION or not isinstance(preview.get("ranked"), list):
        return False
    core = {key: preview.get(key) for key in ("schema_version", "project", "profile", "profile_digest", "ranked")}
    try:
        profile = RetrievalPolicyProfile.model_validate(core["profile"])
    except ValueError:
        return False
    return core["profile_digest"] == profile.digest() and preview.get("preview_digest") == _digest(core)


__all__ = ["SCHEMA_VERSION", "RetrievalPolicyCandidate", "RetrievalPolicyError", "RetrievalPolicyProfile", "build_policy_canary_receipt", "build_retrieval_policy_preview", "verify_retrieval_policy_preview"]
