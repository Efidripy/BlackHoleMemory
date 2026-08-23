"""Local-only admission checks for named external evaluation datasets.

The contract intentionally verifies provenance metadata and local file digests
only. It never downloads, parses, imports, stores or evaluates third-party
dataset content, and it does not authorize a runtime retrieval change.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .filesystem_boundaries import assert_safe_path
from .filesystem_boundaries import read_bytes_safely


SCHEMA_VERSION = "bhm.evaluation.external-dataset-admission.v1"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_DATASET_BYTES = 32 * 1024 * 1024
_MAX_LICENSE_BYTES = 1024 * 1024
_ALLOWED_SUITES = frozenset({"locomo", "longmemeval"})
_REVIEW_STATUS = "approved-local-evaluation-only"


class ExternalEvaluationAdmissionError(ValueError):
    """Raised when a local external evaluation input lacks verifiable metadata."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_text(value: object, field: str, *, limit: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise ExternalEvaluationAdmissionError(f"{field} is required and bounded")
    return text


def _require_digest(value: object, field: str) -> str:
    digest = _require_text(value, field, limit=64).lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ExternalEvaluationAdmissionError(f"{field} must be a SHA-256 digest")
    return digest


def _relative_file(root: Path, value: object, field: str) -> Path:
    relative = Path(_require_text(value, field, limit=240))
    if relative.is_absolute() or ".." in relative.parts:
        raise ExternalEvaluationAdmissionError(f"{field} must be a relative path inside dataset root")
    candidate = assert_safe_path(root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ExternalEvaluationAdmissionError(f"{field} escapes dataset root") from exc
    if not candidate.is_file():
        raise ExternalEvaluationAdmissionError(f"{field} must reference an existing regular file")
    return candidate


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = read_bytes_safely(path, max_bytes=_MAX_MANIFEST_BYTES)
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalEvaluationAdmissionError("admission manifest must be bounded UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ExternalEvaluationAdmissionError("admission manifest root must be an object")
    expected = {"schema_version", "dataset", "review"}
    if set(parsed) != expected:
        raise ExternalEvaluationAdmissionError("admission manifest has unexpected fields")
    return parsed


def _validate_https_source(value: object) -> str:
    source = _require_text(value, "dataset.source_url", limit=2_048)
    parsed = urlparse(source)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise ExternalEvaluationAdmissionError("dataset.source_url must be a credential-free HTTPS URL")
    return source


def _validate_review(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"status", "reviewer", "reviewed_at"}:
        raise ExternalEvaluationAdmissionError("review must contain exactly status, reviewer and reviewed_at")
    if value.get("status") != _REVIEW_STATUS:
        raise ExternalEvaluationAdmissionError("review status is not approved for local evaluation")
    reviewed_at = _require_text(value.get("reviewed_at"), "review.reviewed_at", limit=64)
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExternalEvaluationAdmissionError("review.reviewed_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ExternalEvaluationAdmissionError("review.reviewed_at must include timezone")
    return {"status": _REVIEW_STATUS, "reviewer_digest": _sha256(_require_text(value.get("reviewer"), "review.reviewer", limit=160).encode("utf-8")), "reviewed_at": reviewed_at}


def validate_external_evaluation_dataset_admission(
    dataset_root: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    """Validate one pinned local dataset admission without exposing its content."""

    root = assert_safe_path(Path(dataset_root)).resolve()
    if not root.is_dir():
        raise ExternalEvaluationAdmissionError("dataset root must be an existing directory")
    manifest_file = assert_safe_path(Path(manifest_path)).resolve()
    try:
        manifest_file.relative_to(root)
    except ValueError as exc:
        raise ExternalEvaluationAdmissionError("admission manifest must be inside dataset root") from exc
    manifest = _read_manifest(manifest_file)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ExternalEvaluationAdmissionError("unsupported admission manifest schema")
    dataset = manifest.get("dataset")
    if not isinstance(dataset, dict):
        raise ExternalEvaluationAdmissionError("dataset must be an object")
    expected_dataset = {"suite", "version", "path", "sha256", "source_url", "source_revision", "license"}
    if set(dataset) != expected_dataset:
        raise ExternalEvaluationAdmissionError("dataset has unexpected fields")
    suite = _require_text(dataset.get("suite"), "dataset.suite", limit=40).casefold()
    if suite not in _ALLOWED_SUITES:
        raise ExternalEvaluationAdmissionError("dataset.suite is not supported")
    source_url = _validate_https_source(dataset.get("source_url"))
    source_revision = _require_text(dataset.get("source_revision"), "dataset.source_revision", limit=128).lower()
    if len(source_revision) < 12 or any(character not in "0123456789abcdef" for character in source_revision):
        raise ExternalEvaluationAdmissionError("dataset.source_revision must be a pinned hexadecimal revision")
    data_path = _relative_file(root, dataset.get("path"), "dataset.path")
    if _sha256(read_bytes_safely(data_path, max_bytes=_MAX_DATASET_BYTES)) != _require_digest(dataset.get("sha256"), "dataset.sha256"):
        raise ExternalEvaluationAdmissionError("dataset.sha256 does not match local dataset")
    license_data = dataset.get("license")
    if not isinstance(license_data, dict) or set(license_data) != {"spdx", "evidence_path", "evidence_sha256"}:
        raise ExternalEvaluationAdmissionError("dataset.license has unexpected fields")
    license_path = _relative_file(root, license_data.get("evidence_path"), "dataset.license.evidence_path")
    if _sha256(read_bytes_safely(license_path, max_bytes=_MAX_LICENSE_BYTES)) != _require_digest(license_data.get("evidence_sha256"), "dataset.license.evidence_sha256"):
        raise ExternalEvaluationAdmissionError("dataset.license evidence digest does not match")
    core = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "dataset": {
            "suite": suite,
            "version": _require_text(dataset.get("version"), "dataset.version", limit=120),
            "dataset_digest": _require_digest(dataset.get("sha256"), "dataset.sha256"),
            "source_url_digest": _sha256(source_url.encode("utf-8")),
            "source_revision": source_revision,
            "license_spdx": _require_text(license_data.get("spdx"), "dataset.license.spdx", limit=80),
            "license_evidence_digest": _require_digest(license_data.get("evidence_sha256"), "dataset.license.evidence_sha256"),
        },
        "review": _validate_review(manifest.get("review")),
        "execution": {"network": False, "dataset_content_emitted": False, "model_calls": 0, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False, "runtime_feature_enabled": False},
    }
    core["admission_digest"] = _sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return core


__all__ = ["ExternalEvaluationAdmissionError", "SCHEMA_VERSION", "validate_external_evaluation_dataset_admission"]
