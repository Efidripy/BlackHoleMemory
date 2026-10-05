"""Untrusted, content-free portable-exchange quarantine preview.

This BHM-NG-010 prototype accepts only an already-redacted in-memory portable
bundle and a strict digest-bound manifest.  It is deliberately not an import:
it opens no files, sockets, databases, subprocesses, or projection clients.
SQLite remains the sole authority; foreign evidence stays quarantined until a
separate trust-anchor and import decision is made.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .observation_security import contains_secret_like
from .portable_bundle import (
    AUTHORITY,
    REDACTION_POLICY_VERSION,
    PortableBundleError,
    validate_portable_bundle,
)


MANIFEST_SCHEMA_VERSION = "bhm.portable.exchange-manifest.v1"
PREVIEW_SCHEMA_VERSION = "bhm.portable.exchange-quarantine-preview.v1"
MAX_MANIFEST_BYTES = 16 * 1024
MAX_PENDING_COUNT = 1_000_000

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,119}$")
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_id",
        "bundle_digest",
        "authority",
        "project_scope",
        "source_snapshot_digest",
        "producer_revision",
        "outbox_watermark",
        "projection_generation",
        "redaction_policy_version",
        "created_at",
        "manifest_digest",
    }
)
_WATERMARK_FIELDS = frozenset({"cursor_digest", "pending_count"})
_GENERATION_FIELDS = frozenset({"generation_digest", "projection_included", "rebuild_required"})
_PREVIEW_FIELDS = frozenset(
    {
        "schema_version",
        "preview_id",
        "decision",
        "apply_permitted",
        "trust_state",
        "project_scope",
        "bundle_digest",
        "manifest_digest",
        "source_snapshot_digest",
        "producer_revision_digest",
        "outbox_watermark_digest",
        "projection_generation_digest",
        "counts",
        "projection",
        "execution",
        "rollback",
        "preview_digest",
    }
)
_FORBIDDEN_KEYS = frozenset(
    {
        "content",
        "raw",
        "payload",
        "vector",
        "vectors",
        "embedding",
        "embeddings",
        "secret",
        "token",
        "password",
        "private_key",
        "signature",
        "public_key",
        "trust_anchor",
        "revocation",
        "apply",
        "import",
        "path",
        "file",
        "url",
        "uri",
    }
)


class PortableExchangePreviewError(ValueError):
    """Raised when untrusted exchange evidence violates the preview contract."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_digest(value: Any, *, field: str) -> str:
    text = _require_text(value, field=field, max_chars=64).casefold()
    if not _DIGEST_RE.fullmatch(text):
        raise PortableExchangePreviewError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _require_text(value: Any, *, field: str, max_chars: int = 256) -> str:
    if not isinstance(value, str):
        raise PortableExchangePreviewError(f"{field} must be a bounded string")
    text = value.strip()
    if not text or len(text) > max_chars or contains_secret_like(text):
        raise PortableExchangePreviewError(f"{field} must be a safe bounded string")
    return text


def _require_project(value: Any) -> str:
    project = _require_text(value, field="project_scope", max_chars=120).casefold()
    if not _PROJECT_RE.fullmatch(project):
        raise PortableExchangePreviewError("project_scope must be a safe single label")
    return project


def _require_label(value: Any, *, field: str) -> str:
    label = _require_text(value, field=field, max_chars=120).casefold()
    if not _LABEL_RE.fullmatch(label):
        raise PortableExchangePreviewError(f"{field} must be a safe opaque label")
    return label


def _require_timestamp(value: Any) -> str:
    raw = _require_text(value, field="created_at", max_chars=64)
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PortableExchangePreviewError("created_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PortableExchangePreviewError("created_at must include a timezone")
    return parsed.isoformat().replace("+00:00", "Z")


def _assert_safe_tree(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS:
                raise PortableExchangePreviewError("forbidden exchange field")
            _assert_safe_tree(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _assert_safe_tree(child)
    elif isinstance(value, str) and contains_secret_like(value):
        raise PortableExchangePreviewError("secret-like or path-bearing exchange value")


def _bounded_bytes(value: Mapping[str, Any]) -> None:
    if len((_canonical_json(value) + "\n").encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise PortableExchangePreviewError("manifest exceeds bounded size")


def _validated_manifest(manifest: Mapping[str, Any], *, bundle_receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise PortableExchangePreviewError("manifest must be an object")
    _bounded_bytes(manifest)
    if set(manifest) != _MANIFEST_FIELDS:
        raise PortableExchangePreviewError("manifest fields do not match the exchange contract")
    _assert_safe_tree(manifest)
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise PortableExchangePreviewError("unsupported exchange manifest schema")
    if manifest["authority"] != AUTHORITY:
        raise PortableExchangePreviewError("manifest authority must remain sqlite-authoritative")
    if manifest["redaction_policy_version"] != REDACTION_POLICY_VERSION:
        raise PortableExchangePreviewError("unsupported redaction policy")
    manifest_id = _require_label(manifest["manifest_id"], field="manifest_id")
    project = _require_project(manifest["project_scope"])
    bundle_digest = _require_digest(manifest["bundle_digest"], field="bundle_digest")
    source_digest = _require_digest(manifest["source_snapshot_digest"], field="source_snapshot_digest")
    producer_revision = _require_text(manifest["producer_revision"], field="producer_revision", max_chars=256)
    created_at = _require_timestamp(manifest["created_at"])

    watermark = manifest["outbox_watermark"]
    if not isinstance(watermark, Mapping) or set(watermark) != _WATERMARK_FIELDS:
        raise PortableExchangePreviewError("outbox_watermark must be a bounded opaque cursor")
    cursor_digest = _require_digest(watermark["cursor_digest"], field="outbox_watermark.cursor_digest")
    pending_count = watermark["pending_count"]
    if isinstance(pending_count, bool) or not isinstance(pending_count, int) or not 0 <= pending_count <= MAX_PENDING_COUNT:
        raise PortableExchangePreviewError("outbox_watermark.pending_count is out of bounds")

    generation = manifest["projection_generation"]
    if not isinstance(generation, Mapping) or set(generation) != _GENERATION_FIELDS:
        raise PortableExchangePreviewError("projection_generation must be a strict rebuild declaration")
    generation_digest = _require_digest(generation["generation_digest"], field="projection_generation.generation_digest")
    if generation["projection_included"] is not False or generation["rebuild_required"] is not True:
        raise PortableExchangePreviewError("foreign projections must be excluded and rebuilt")

    without_digest = dict(manifest)
    manifest_digest = _require_digest(without_digest.pop("manifest_digest"), field="manifest_digest")
    if manifest_digest != _digest(without_digest):
        raise PortableExchangePreviewError("manifest digest mismatch")
    if (
        bundle_digest != bundle_receipt["bundle_digest"]
        or project != bundle_receipt["project_scope"]
        or source_digest != bundle_receipt["source_snapshot_digest"]
        or producer_revision != bundle_receipt["producer_revision"]
    ):
        raise PortableExchangePreviewError("manifest does not bind to the validated bundle")
    return {
        "manifest_id": manifest_id,
        "manifest_digest": manifest_digest,
        "project_scope": project,
        "bundle_digest": bundle_digest,
        "source_snapshot_digest": source_digest,
        "producer_revision": producer_revision,
        "created_at": created_at,
        "outbox_watermark": {"cursor_digest": cursor_digest, "pending_count": pending_count},
        "projection_generation": {"generation_digest": generation_digest, "projection_included": False, "rebuild_required": True},
    }


def build_portable_exchange_quarantine_preview(
    bundle: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate foreign evidence and return a deterministic no-write quarantine receipt."""

    try:
        bundle_receipt = validate_portable_bundle(bundle)
    except PortableBundleError as exc:
        raise PortableExchangePreviewError("portable bundle validation failed") from exc
    bundle_contract = {
        "bundle_digest": bundle_receipt["bundle_digest"],
        "project_scope": bundle_receipt["project_scope"],
        "source_snapshot_digest": bundle.get("source_snapshot_digest"),
        "producer_revision": bundle.get("producer_revision"),
    }
    _require_digest(bundle_contract["source_snapshot_digest"], field="bundle.source_snapshot_digest")
    _require_text(bundle_contract["producer_revision"], field="bundle.producer_revision", max_chars=256)
    validated = _validated_manifest(manifest, bundle_receipt=bundle_contract)
    receipt: dict[str, Any] = {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "preview_id": f"quarantine-preview:{_digest({'bundle': validated['bundle_digest'], 'manifest': validated['manifest_digest']})[:24]}",
        "decision": "quarantine",
        "apply_permitted": False,
        "trust_state": "untrusted-no-signature-decision",
        "project_scope": validated["project_scope"],
        "bundle_digest": validated["bundle_digest"],
        "manifest_digest": validated["manifest_digest"],
        "source_snapshot_digest": validated["source_snapshot_digest"],
        "producer_revision_digest": _digest(validated["producer_revision"]),
        "outbox_watermark_digest": _digest(validated["outbox_watermark"]),
        "projection_generation_digest": _digest(validated["projection_generation"]),
        "counts": dict(bundle_receipt["counts"]),
        "projection": {"included": False, "rebuild_required": True, "authority": "not-imported"},
        "execution": {
            "writes_sqlite": False,
            "writes_outbox": False,
            "writes_mem0": False,
            "writes_qdrant": False,
            "writes_graph": False,
            "writes_runtime": False,
            "network": False,
            "filesystem": False,
            "subprocess": False,
        },
        "rollback": {"action": "discard_preview", "persistent_state_created": False},
    }
    receipt["preview_digest"] = _digest(receipt)
    return receipt


def verify_portable_exchange_preview(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed when a quarantine-preview receipt is malformed or tampered."""

    if not isinstance(receipt, Mapping) or set(receipt) != _PREVIEW_FIELDS:
        raise PortableExchangePreviewError("preview fields do not match the exchange contract")
    _assert_safe_tree(receipt)
    if (
        receipt["schema_version"] != PREVIEW_SCHEMA_VERSION
        or receipt["decision"] != "quarantine"
        or receipt["apply_permitted"] is not False
        or receipt["trust_state"] != "untrusted-no-signature-decision"
    ):
        raise PortableExchangePreviewError("preview must remain an untrusted quarantine")
    preview_id = _require_text(receipt["preview_id"], field="preview_id", max_chars=160)
    if not preview_id.startswith("quarantine-preview:"):
        raise PortableExchangePreviewError("preview_id must remain a quarantine identity")
    _require_label(preview_id.removeprefix("quarantine-preview:"), field="preview_id")
    _require_project(receipt["project_scope"])
    for field in (
        "bundle_digest",
        "manifest_digest",
        "source_snapshot_digest",
        "producer_revision_digest",
        "outbox_watermark_digest",
        "projection_generation_digest",
        "preview_digest",
    ):
        _require_digest(receipt[field], field=field)
    if receipt["projection"] != {"included": False, "rebuild_required": True, "authority": "not-imported"}:
        raise PortableExchangePreviewError("preview projection boundary changed")
    expected_execution = {
        "writes_sqlite": False,
        "writes_outbox": False,
        "writes_mem0": False,
        "writes_qdrant": False,
        "writes_graph": False,
        "writes_runtime": False,
        "network": False,
        "filesystem": False,
        "subprocess": False,
    }
    if receipt["execution"] != expected_execution or receipt["rollback"] != {"action": "discard_preview", "persistent_state_created": False}:
        raise PortableExchangePreviewError("preview execution boundary changed")
    counts = receipt["counts"]
    if not isinstance(counts, Mapping) or set(counts) != {"memories", "links", "artifacts", "provenance"}:
        raise PortableExchangePreviewError("preview counts are invalid")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
        raise PortableExchangePreviewError("preview counts are invalid")
    without_digest = dict(receipt)
    preview_digest = without_digest.pop("preview_digest")
    if preview_digest != _digest(without_digest):
        raise PortableExchangePreviewError("preview digest mismatch")
    return {"valid": True, "decision": "quarantine", "apply_permitted": False, "preview_digest": preview_digest}


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "PREVIEW_SCHEMA_VERSION",
    "PortableExchangePreviewError",
    "build_portable_exchange_quarantine_preview",
    "verify_portable_exchange_preview",
]
