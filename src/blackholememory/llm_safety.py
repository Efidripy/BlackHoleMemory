"""Privacy and authority envelope for local-LLM ingress and outputs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .filesystem_boundaries import FilesystemBoundaryError
from .filesystem_boundaries import assert_safe_path
from .observation_security import PayloadSanitizer


LLM_SAFETY_POLICY_VERSION = "bhm.llm.safety.v1"
LLM_SAFETY_MAX_INPUT_BYTES = 256 * 1024
LLM_SAFETY_MAX_SANITIZED_BYTES = 128 * 1024
LLM_SAFETY_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
PROPOSAL_AUTHORITY = "proposal"

_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous_instructions", re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?\b", re.I)),
    ("prompt_exfiltration", re.compile(r"\b(?:reveal|show|print|dump)\s+(?:the\s+)?(?:system|developer)\s+prompt\b", re.I)),
    ("secret_exfiltration", re.compile(r"\b(?:reveal|show|print|dump|export)\b[^\n]{0,80}\b(?:secret|token|api[_ -]?key|password)\b", re.I)),
    ("safety_bypass", re.compile(r"\b(?:disable|bypass|ignore|circumvent)\s+(?:safety|security|policy|guard)\b", re.I)),
    ("role_override", re.compile(r"\b(?:you are now|act as|new system message|developer message)\b", re.I)),
    ("markup_boundary", re.compile(r"(?:<\/?system>|<\/?developer>|\[SYSTEM\]|\[DEVELOPER\])", re.I)),
)
_SECRET_PATH_SUFFIXES = {".env", ".pem", ".key", ".p12", ".pfx", ".kdbx"}
_SECRET_PATH_PARTS = {".git", "secrets", "credentials", "tokens", "private"}


class LLMSafetyViolation(ValueError):
    pass


@dataclass(frozen=True)
class SafetyTransform:
    value: Any
    provenance: dict[str, Any]


def sanitize_llm_value(
    value: Any,
    *,
    source: str = "unknown",
    project: str = "blackholememory",
    max_input_bytes: int = LLM_SAFETY_MAX_INPUT_BYTES,
    max_sanitized_bytes: int = LLM_SAFETY_MAX_SANITIZED_BYTES,
) -> SafetyTransform:
    raw_json = _canonical_json(value)
    input_bytes = len(raw_json.encode("utf-8"))
    if input_bytes > max_input_bytes:
        raise LLMSafetyViolation("LLM ingress exceeds input byte limit")
    sanitizer = PayloadSanitizer(max_string_chars=16_384, max_collection_items=256, max_depth=16)
    sanitized = sanitizer.sanitize(value)
    sanitized_json = _canonical_json(sanitized)
    sanitized_bytes = len(sanitized_json.encode("utf-8"))
    if sanitized_bytes > max_sanitized_bytes:
        raise LLMSafetyViolation("LLM ingress exceeds sanitized byte limit")
    return SafetyTransform(
        value=sanitized,
        provenance={
            "policy_version": LLM_SAFETY_POLICY_VERSION,
            "source": str(source)[:120],
            "project": str(project)[:120],
            "input_sha256": _sha256(raw_json),
            "sanitized_sha256": _sha256(sanitized_json),
            "input_bytes": input_bytes,
            "sanitized_bytes": sanitized_bytes,
            "redaction_count": sanitizer.redaction_count,
            "redaction_kinds": sorted(sanitizer.redaction_kinds),
            "truncated_strings": sanitizer.truncated_strings,
            "dropped_items": sanitizer.dropped_items,
            "depth_limit_hits": sanitizer.depth_limit_hits,
        },
    )


def sanitize_llm_messages(
    messages: Iterable[dict[str, Any]],
    *,
    source: str = "local-llm-gateway",
    project: str = "blackholememory",
) -> SafetyTransform:
    raw_messages = [dict(message) for message in messages]
    transformed = sanitize_llm_value(raw_messages, source=source, project=project)
    safe_messages = []
    findings: set[str] = set()
    for message in transformed.value:
        role = str(message.get("role") or "")
        content = message.get("content")
        if role in {"user", "tool"}:
            findings.update(scan_prompt_injection(_flatten_text(content)))
            message = dict(message)
            message["content"] = _wrap_untrusted(content)
        safe_messages.append(message)
    provenance = dict(transformed.provenance)
    provenance.update(
        {
            "injection_findings": sorted(findings),
            "untrusted_roles": ["user", "tool"],
            "authority": PROPOSAL_AUTHORITY,
            "auto_apply": False,
            "requires_validation": True,
        }
    )
    return SafetyTransform(value=tuple(safe_messages), provenance=provenance)


def scan_prompt_injection(text: str) -> tuple[str, ...]:
    normalized = str(text or "")
    return tuple(sorted(name for name, pattern in _INJECTION_RULES if pattern.search(normalized)))


def build_proposal_envelope(
    *,
    job_id: str,
    output: Any,
    provenance: dict[str, Any],
    validator: str = "deterministic-validator-required",
) -> dict[str, Any]:
    safe_output = sanitize_llm_value(output, source="local-llm-output", project=provenance.get("project", "blackholememory"))
    output_json = _canonical_json(safe_output.value)
    proposal_id = f"proposal_{_sha256(f'{job_id}:{output_json}')[:24]}"
    merged_provenance = dict(provenance)
    merged_provenance["output"] = safe_output.provenance
    merged_provenance["authority"] = PROPOSAL_AUTHORITY
    return {
        "schema_version": LLM_SAFETY_POLICY_VERSION,
        "proposal_id": proposal_id,
        "job_id": str(job_id),
        "candidate": safe_output.value,
        "authority": PROPOSAL_AUTHORITY,
        "auto_apply": False,
        "requires_validation": True,
        "requires_approval": True,
        "validator": str(validator)[:200],
        "provenance": merged_provenance,
    }


def allowlisted_artifact_manifest(paths: Iterable[str | Path], roots: Iterable[str | Path]) -> list[dict[str, Any]]:
    resolved_roots: list[Path] = []
    for root in roots:
        lexical_root = Path(root).expanduser()
        try:
            assert_safe_path(lexical_root, reject_hardlink_target=False)
            resolved_root = lexical_root.resolve()
            assert_safe_path(resolved_root, reject_hardlink_target=False)
        except FilesystemBoundaryError as exc:
            raise LLMSafetyViolation(f"artifact allowlist root crosses a filesystem boundary: {root}") from exc
        resolved_roots.append(resolved_root)
    if not resolved_roots:
        raise LLMSafetyViolation("artifact allowlist cannot be empty")
    manifest: list[dict[str, Any]] = []
    for raw_path in paths:
        lexical_path = Path(raw_path).expanduser()
        try:
            assert_safe_path(lexical_path)
            path = lexical_path.resolve()
            assert_safe_path(path)
        except FilesystemBoundaryError as exc:
            raise LLMSafetyViolation(f"artifact crosses a filesystem boundary: {raw_path}") from exc
        if not any(_is_relative_to(path, root) for root in resolved_roots):
            raise LLMSafetyViolation(f"artifact is outside allowlisted roots: {raw_path}")
        if (
            path.suffix.casefold() in _SECRET_PATH_SUFFIXES
            or path.name.casefold().startswith(".env")
            or any(part.casefold() in _SECRET_PATH_PARTS for part in path.parts)
        ):
            raise LLMSafetyViolation(f"artifact path is sensitive: {raw_path}")
        if not path.is_file():
            raise LLMSafetyViolation(f"artifact is not a file: {raw_path}")
        size = path.stat().st_size
        if size > LLM_SAFETY_MAX_ARTIFACT_BYTES:
            raise LLMSafetyViolation(f"artifact exceeds size limit: {raw_path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.append(
            {
                "path": str(path),
                "size_bytes": size,
                "sha256": digest,
                "allowlisted": True,
            }
        )
    return manifest


def _wrap_untrusted(value: Any) -> Any:
    if isinstance(value, str):
        return f"[UNTRUSTED_DATA_BEGIN]\n{value}\n[UNTRUSTED_DATA_END]"
    if isinstance(value, list):
        return [_wrap_untrusted(item) for item in value]
    if isinstance(value, dict):
        result = dict(value)
        if str(result.get("type") or "") == "image_url":
            return result
        if isinstance(result.get("text"), str):
            result["text"] = _wrap_untrusted(result["text"])
        return result
    return value


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    return ""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "LLM_SAFETY_MAX_ARTIFACT_BYTES",
    "LLM_SAFETY_MAX_INPUT_BYTES",
    "LLM_SAFETY_MAX_SANITIZED_BYTES",
    "LLM_SAFETY_POLICY_VERSION",
    "LLMSafetyViolation",
    "PROPOSAL_AUTHORITY",
    "SafetyTransform",
    "allowlisted_artifact_manifest",
    "build_proposal_envelope",
    "sanitize_llm_messages",
    "sanitize_llm_value",
    "scan_prompt_injection",
]
