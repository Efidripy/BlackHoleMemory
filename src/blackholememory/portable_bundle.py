"""Disposable-first redacted portable memory bundle contract.

This module deliberately does not read or write the live SQLite/Qdrant stores.
Callers provide a bounded snapshot projection and receive a deterministic,
redacted envelope suitable for transfer or a dry-run import preview.  The
bundle is evidence, never a second authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .observation_security import contains_secret_like, secure_observation_payload


SCHEMA_VERSION = "bhm.portable.redacted-bundle.v1"
REDACTION_POLICY_VERSION = "bhm.redaction.v1"
AUTHORITY = "sqlite-authoritative"
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_FORBIDDEN_KEYS = frozenset(
    {
        "content",
        "raw",
        "raw_payload",
        "vector",
        "vectors",
        "runtime_log",
        "runtime_logs",
        "token",
        "secret",
        "password",
        "cookie",
        "authorization",
        "api_key",
        "private_key",
    }
)
_SECTION_NAMES = ("memories", "links", "artifacts", "provenance")
_ARTIFACT_TYPES = frozenset(
    {
        "checkpoint",
        "project_map",
        "adr",
        "handoff",
        "session_record",
        "task",
        "task_context",
        "risk_register",
        "validation_snapshot",
    }
)


class PortableBundleError(ValueError):
    """Raised when a portable bundle violates its fail-closed contract."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: Any, *, field: str, max_chars: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_chars:
        raise PortableBundleError(f"{field} must be a non-empty bounded string")
    return text


def _project(value: Any) -> str:
    project = _require_text(value, field="project", max_chars=120).casefold()
    if not _PROJECT_RE.fullmatch(project):
        raise PortableBundleError("project must be a safe single label")
    return project


def _require_digest(value: Any, *, field: str) -> str:
    digest = _require_text(value, field=field, max_chars=64).casefold()
    if not _DIGEST_RE.fullmatch(digest):
        raise PortableBundleError(f"{field} must be a lowercase SHA-256 digest")
    return digest


def _require_timestamp(value: Any) -> str:
    raw = _require_text(value, field="created_at", max_chars=64)
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PortableBundleError("created_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PortableBundleError("created_at must include a timezone")
    return parsed.isoformat().replace("+00:00", "Z")


def _ref(prefix: str, value: Any) -> str:
    return f"{prefix}:{_digest_text(str(value))[:24]}"


def _metadata_projection(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata")
    if metadata is None:
        metadata = item.get("metadata_json", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {"metadata_parse_failed": True}
    if not isinstance(metadata, Mapping):
        metadata = {}
    flags = {
        key: bool(metadata.get(key))
        for key in ("pinned", "archived", "contradicted", "superseded")
    }
    sanitized = secure_observation_payload({"metadata": dict(metadata)})["metadata"]
    return {"flags": flags, "metadata_digest": _digest(sanitized)}


def _memory_projection(item: Mapping[str, Any], *, project: str) -> dict[str, Any]:
    item_project = _project(item.get("project", project))
    if item_project != project:
        raise PortableBundleError("memory crosses project scope")
    memory_id = _require_text(item.get("memory_id") or item.get("id"), field="memory_id", max_chars=512)
    revision = item.get("current_revision_id") or item.get("revision_id")
    result: dict[str, Any] = {
        "memory_ref": _ref("memory", memory_id),
        "project": project,
        "lifecycle": _require_text(item.get("lifecycle", "active"), field="lifecycle", max_chars=32),
        "metadata": _metadata_projection(item),
    }
    if revision:
        result["revision_ref"] = _ref("revision", revision)
    updated_at = item.get("updated_at")
    if updated_at is not None:
        result["updated_at"] = _require_text(updated_at, field="updated_at", max_chars=64)
    return result


def _link_projection(item: Mapping[str, Any], *, project: str) -> dict[str, Any]:
    item_project = _project(item.get("project", project))
    if item_project != project:
        raise PortableBundleError("link crosses project scope")
    source = _require_text(item.get("source_id") or item.get("source"), field="source_id")
    target = _require_text(item.get("target_id") or item.get("target"), field="target_id")
    relation = _require_text(item.get("relation", "related"), field="relation", max_chars=64).casefold()
    return {
        "project": project,
        "source_ref": _ref("memory", source),
        "target_ref": _ref("memory", target),
        "relation": relation,
    }


def _artifact_projection(item: Mapping[str, Any], *, project: str) -> dict[str, Any]:
    item_project = _project(item.get("project", project))
    if item_project != project:
        raise PortableBundleError("artifact crosses project scope")
    artifact_type = _require_text(item.get("artifact_type") or item.get("type"), field="artifact_type", max_chars=64)
    if artifact_type not in _ARTIFACT_TYPES:
        raise PortableBundleError(f"unsupported artifact type: {artifact_type}")
    artifact_id = _require_text(item.get("artifact_id") or item.get("id"), field="artifact_id")
    result = {
        "project": project,
        "artifact_type": artifact_type,
        "artifact_ref": _ref("artifact", artifact_id),
    }
    memory_id = item.get("memory_id")
    if memory_id:
        result["memory_ref"] = _ref("memory", memory_id)
    metadata = item.get("metadata", {})
    result["metadata_digest"] = _digest(secure_observation_payload({"metadata": metadata})["metadata"])
    return result


def _provenance_projection(item: Mapping[str, Any], *, project: str) -> dict[str, Any]:
    item_project = _project(item.get("project", project))
    if item_project != project:
        raise PortableBundleError("provenance crosses project scope")
    source = _require_text(item.get("source") or item.get("source_id"), field="source")
    result = {"project": project, "source_ref": _ref("source", source)}
    for key in ("revision", "source_digest", "evidence_class"):
        value = item.get(key)
        if value is not None:
            result[key] = _require_text(value, field=key, max_chars=512)
    return result


def _section(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(items), "digest": _digest(items), "items": items}


def _iter_values(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _iter_values(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _iter_values(child)


def _assert_safe_payload(value: Any) -> None:
    for key, child in _iter_values(value):
        if key.casefold() in _FORBIDDEN_KEYS:
            raise PortableBundleError(f"forbidden field in portable bundle: {key}")
        if isinstance(child, str) and contains_secret_like(child):
            raise PortableBundleError("secret-like or path-bearing value in portable bundle")


def build_portable_bundle(
    snapshot: Mapping[str, Any],
    *,
    project: str,
    producer_revision: str,
    source_snapshot_digest: str,
    created_at: str,
    bundle_id: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic redacted bundle from a disposable snapshot."""

    canonical_project = _project(project)
    revision = _require_text(producer_revision, field="producer_revision", max_chars=256)
    source_digest = _require_digest(source_snapshot_digest, field="source_snapshot_digest")
    timestamp = _require_timestamp(created_at)
    if not isinstance(snapshot, Mapping):
        raise PortableBundleError("snapshot must be an object")
    raw_memories = snapshot.get("memories", [])
    raw_links = snapshot.get("links", [])
    raw_artifacts = snapshot.get("artifacts", [])
    raw_provenance = snapshot.get("provenance", [])
    collections = (raw_memories, raw_links, raw_artifacts, raw_provenance)
    if any(not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)) for items in collections):
        raise PortableBundleError("snapshot sections must be arrays")
    sections = {
        "memories": _section([_memory_projection(item, project=canonical_project) for item in raw_memories if isinstance(item, Mapping)]),
        "links": _section([_link_projection(item, project=canonical_project) for item in raw_links if isinstance(item, Mapping)]),
        "artifacts": _section([_artifact_projection(item, project=canonical_project) for item in raw_artifacts if isinstance(item, Mapping)]),
        "provenance": _section([_provenance_projection(item, project=canonical_project) for item in raw_provenance if isinstance(item, Mapping)]),
    }
    if any(not isinstance(item, Mapping) for section in collections for item in section):
        raise PortableBundleError("snapshot section items must be objects")
    generated_id = bundle_id or f"bundle:{_digest_text(f'{canonical_project}|{source_digest}|{revision}|{timestamp}')[:24]}"
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": _require_text(generated_id, field="bundle_id", max_chars=128),
        "created_at": timestamp,
        "producer_revision": revision,
        "authority": AUTHORITY,
        "project_scope": canonical_project,
        "source_snapshot_digest": source_digest,
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "sections": sections,
        "compatibility": {"writes_sqlite": False, "writes_qdrant": False, "qdrant_included": False},
    }
    _assert_safe_payload(envelope)
    envelope["bundle_digest"] = _digest(envelope)
    return envelope


def validate_portable_bundle(bundle: Mapping[str, Any], *, max_bytes: int = MAX_BUNDLE_BYTES) -> dict[str, Any]:
    """Validate integrity, redaction and ownership without performing writes."""

    if not isinstance(bundle, Mapping):
        raise PortableBundleError("bundle must be an object")
    encoded = (_canonical_json(bundle) + "\n").encode("utf-8")
    if len(encoded) > max_bytes:
        raise PortableBundleError("bundle exceeds bounded size")
    required = {
        "schema_version",
        "bundle_id",
        "created_at",
        "producer_revision",
        "authority",
        "project_scope",
        "source_snapshot_digest",
        "redaction_policy_version",
        "sections",
        "compatibility",
        "bundle_digest",
    }
    if set(bundle) != required:
        raise PortableBundleError("bundle fields do not match v1 contract")
    if bundle["schema_version"] != SCHEMA_VERSION or bundle["authority"] != AUTHORITY:
        raise PortableBundleError("unsupported bundle schema or authority")
    project = _project(bundle["project_scope"])
    _require_text(bundle["bundle_id"], field="bundle_id", max_chars=128)
    _require_timestamp(bundle["created_at"])
    _require_text(bundle["producer_revision"], field="producer_revision", max_chars=256)
    _require_digest(bundle["source_snapshot_digest"], field="source_snapshot_digest")
    if bundle["redaction_policy_version"] != REDACTION_POLICY_VERSION:
        raise PortableBundleError("unsupported redaction policy")
    compatibility = bundle["compatibility"]
    if compatibility != {"writes_sqlite": False, "writes_qdrant": False, "qdrant_included": False}:
        raise PortableBundleError("portable bundle compatibility must remain dry-run only")
    sections = bundle["sections"]
    if not isinstance(sections, Mapping) or set(sections) != set(_SECTION_NAMES):
        raise PortableBundleError("bundle sections do not match v1 allowlist")
    counts: dict[str, int] = {}
    for name in _SECTION_NAMES:
        section = sections[name]
        if not isinstance(section, Mapping) or set(section) != {"count", "digest", "items"}:
            raise PortableBundleError(f"invalid {name} section")
        items = section["items"]
        if not isinstance(items, list) or section["count"] != len(items):
            raise PortableBundleError(f"invalid {name} section count")
        if section["digest"] != _digest(items):
            raise PortableBundleError(f"invalid {name} section digest")
        for item in items:
            if not isinstance(item, Mapping) or item.get("project") != project:
                raise PortableBundleError(f"{name} contains foreign project data")
        counts[name] = len(items)
    _assert_safe_payload({"sections": sections})
    without_digest = dict(bundle)
    without_digest.pop("bundle_digest")
    if bundle["bundle_digest"] != _digest(without_digest):
        raise PortableBundleError("bundle digest mismatch")
    return {
        "valid": True,
        "schema_version": SCHEMA_VERSION,
        "project_scope": project,
        "bundle_digest": bundle["bundle_digest"],
        "counts": counts,
        "writes_sqlite": False,
        "writes_qdrant": False,
        "action": "dry-run",
    }


def dry_run_import(bundle: Mapping[str, Any], *, max_bytes: int = MAX_BUNDLE_BYTES) -> dict[str, Any]:
    """Return a no-write import receipt after complete bundle validation."""

    return validate_portable_bundle(bundle, max_bytes=max_bytes)


__all__ = [
    "AUTHORITY",
    "MAX_BUNDLE_BYTES",
    "PortableBundleError",
    "REDACTION_POLICY_VERSION",
    "SCHEMA_VERSION",
    "build_portable_bundle",
    "dry_run_import",
    "validate_portable_bundle",
]
