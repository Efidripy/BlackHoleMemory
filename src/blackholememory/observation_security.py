from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal


OBSERVATION_SECURITY_POLICY_VERSION = "1.0"
OBSERVATION_MAX_INPUT_BYTES = 256 * 1024
OBSERVATION_COMPACT_MAX_INPUT_BYTES = 512 * 1024
OBSERVATION_IDLE_MAX_INPUT_BYTES = 64 * 1024
OBSERVATION_MAX_SANITIZED_BYTES = 64 * 1024
OBSERVATION_MAX_STRING_CHARS = 4096
OBSERVATION_MAX_KEY_CHARS = 256
OBSERVATION_MAX_COLLECTION_ITEMS = 128
OBSERVATION_MAX_DEPTH = 12

ObservationSensitivity = Literal["public", "internal", "restricted"]

_REDACTED_PREFIX = "[REDACTED:"
_TRUNCATION_SUFFIX = "...[TRUNCATED]"

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "id_token",
    "bearer_token",
    "client_secret",
    "secret",
    "password",
    "passwd",
    "pwd",
    "credential",
    "credentials",
    "private_key",
    "ssh_private_key",
    "cookie",
    "set_cookie",
}
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_access_token",
    "_refresh_token",
    "_auth_token",
    "_client_secret",
    "_password",
    "_passwd",
    "_credential",
    "_credentials",
    "_private_key",
    "_cookie",
)


@dataclass(frozen=True)
class TextRedactionResult:
    value: str
    replacements: int
    kinds: tuple[str, ...]


@dataclass
class PayloadSanitizer:
    max_string_chars: int = OBSERVATION_MAX_STRING_CHARS
    max_key_chars: int = OBSERVATION_MAX_KEY_CHARS
    max_collection_items: int = OBSERVATION_MAX_COLLECTION_ITEMS
    max_depth: int = OBSERVATION_MAX_DEPTH
    redaction_count: int = 0
    redaction_kinds: Counter[str] = field(default_factory=Counter)
    truncated_strings: int = 0
    truncated_keys: int = 0
    dropped_items: int = 0
    depth_limit_hits: int = 0

    def sanitize(self, value: Any, *, depth: int = 0) -> Any:
        if depth > self.max_depth:
            self.depth_limit_hits += 1
            return "[TRUNCATED:max-depth]"

        if value is None or isinstance(value, (bool, int)):
            return value

        if isinstance(value, float):
            if math.isfinite(value):
                return value
            self.redaction_count += 1
            self.redaction_kinds["non-finite-number"] += 1
            return "[REDACTED:non-finite-number]"

        if isinstance(value, str):
            return self._sanitize_string(value)

        if isinstance(value, dict):
            result: dict[str, Any] = {}
            items = list(value.items())
            if len(items) > self.max_collection_items:
                self.dropped_items += len(items) - self.max_collection_items
                items = items[: self.max_collection_items]
            for raw_key, raw_value in items:
                key = self._sanitize_key(raw_key)
                if self._is_sensitive_key(key):
                    if isinstance(raw_value, str) and raw_value.startswith(_REDACTED_PREFIX):
                        result[key] = raw_value
                    else:
                        result[key] = "[REDACTED:sensitive-key]"
                        self.redaction_count += 1
                        self.redaction_kinds["sensitive-key"] += 1
                    continue
                result[key] = self.sanitize(raw_value, depth=depth + 1)
            return result

        if isinstance(value, (list, tuple, set)):
            items = list(value)
            if len(items) > self.max_collection_items:
                self.dropped_items += len(items) - self.max_collection_items
                items = items[: self.max_collection_items]
            return [self.sanitize(item, depth=depth + 1) for item in items]

        return self._sanitize_string(str(value))

    def report(self, *, input_bytes: int, sanitized_bytes: int, input_payload_state: str) -> dict[str, Any]:
        return {
            "policyVersion": OBSERVATION_SECURITY_POLICY_VERSION,
            "sensitivity": "restricted" if self.redaction_count else "internal",
            "inputPayloadState": input_payload_state,
            "inputBytes": input_bytes,
            "sanitizedBytes": sanitized_bytes,
            "redactionCount": self.redaction_count,
            "redactionKinds": sorted(self.redaction_kinds),
            "truncatedStrings": self.truncated_strings,
            "truncatedKeys": self.truncated_keys,
            "droppedItems": self.dropped_items,
            "depthLimitHits": self.depth_limit_hits,
            "limits": {
                "maxSanitizedBytes": OBSERVATION_MAX_SANITIZED_BYTES,
                "maxStringChars": self.max_string_chars,
                "maxKeyChars": self.max_key_chars,
                "maxCollectionItems": self.max_collection_items,
                "maxDepth": self.max_depth,
            },
        }

    def _sanitize_string(self, value: str) -> str:
        redacted = redact_secret_text(value)
        if redacted.replacements:
            self.redaction_count += redacted.replacements
            for kind in redacted.kinds:
                self.redaction_kinds[kind] += 1
        result = redacted.value
        if len(result) > self.max_string_chars:
            keep = max(self.max_string_chars - len(_TRUNCATION_SUFFIX), 0)
            result = result[:keep] + _TRUNCATION_SUFFIX
            self.truncated_strings += 1
        return result

    def _sanitize_key(self, value: Any) -> str:
        key = str(value)
        if len(key) <= self.max_key_chars:
            return key
        digest = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:12]
        keep = max(self.max_key_chars - len(digest) - 15, 0)
        self.truncated_keys += 1
        return f"{key[:keep]}...[KEY:{digest}]"

    @staticmethod
    def _is_sensitive_key(value: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
        return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


class ObservationPayloadTooLarge(ValueError):
    def __init__(self, *, stage: str, actual_bytes: int, limit_bytes: int) -> None:
        self.stage = stage
        self.actual_bytes = actual_bytes
        self.limit_bytes = limit_bytes
        super().__init__(f"observation payload exceeds {stage} limit")

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": "observation_payload_too_large",
            "stage": self.stage,
            "actualBytes": self.actual_bytes,
            "limitBytes": self.limit_bytes,
        }


def _replace_pattern(value: str, pattern: re.Pattern[str], replacement: str | Any, kind: str) -> tuple[str, int, str]:
    updated, count = pattern.subn(replacement, value)
    return updated, count, kind


_TEXT_REDACTION_RULES: tuple[tuple[re.Pattern[str], str | Any, str], ...] = (
    (
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
            re.IGNORECASE,
        ),
        "[REDACTED:private-key]",
        "private-key",
    ),
    (
        re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
        lambda match: f"{match.group(1)} [REDACTED:authorization]",
        "authorization",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        "[REDACTED:known-token]",
        "known-token",
    ),
    (
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.IGNORECASE),
        "[REDACTED:known-token]",
        "known-token",
    ),
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED:cloud-access-key]",
        "cloud-access-key",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "[REDACTED:jwt]",
        "jwt",
    ),
    (
        re.compile(
            r"\b([A-Z][A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|CLIENT_SECRET|PASSWORD|PRIVATE_KEY))"
            r"\s*=(?!(?:\s*)\[REDACTED:)\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        ),
        lambda match: f"{match.group(1)}=[REDACTED:env-secret]",
        "env-secret",
    ),
    (
        re.compile(
            r"\b(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|client[_-]?secret|"
            r"password|passwd|pwd|secret|token|authorization)[\"']?\s*[:=](?!(?:\s*)\[REDACTED:)\s*"
            r"(?:\"[^\"]{4,}\"|'[^']{4,}'|[^\s,;]{8,})",
            re.IGNORECASE,
        ),
        lambda match: f"{match.group(1)}=[REDACTED:secret-assignment]",
        "secret-assignment",
    ),
    (
        re.compile(r"\b(https?://)([^:/@\s]+):(?!\[REDACTED:)([^@\s]+)@", re.IGNORECASE),
        lambda match: f"{match.group(1)}{match.group(2)}:[REDACTED:url-credential]@",
        "url-credential",
    ),
    (
        re.compile(r"\b(set-cookie|cookie)\s*:(?!(?:\s*)\[REDACTED:)\s*[^\r\n]+", re.IGNORECASE),
        lambda match: f"{match.group(1)}: [REDACTED:cookie]",
        "cookie",
    ),
)


def redact_secret_text(value: str) -> TextRedactionResult:
    redacted = str(value)
    replacements = 0
    kinds: list[str] = []
    for pattern, replacement, kind in _TEXT_REDACTION_RULES:
        redacted, count, _ = _replace_pattern(redacted, pattern, replacement, kind)
        if count:
            replacements += count
            kinds.extend([kind] * count)
    return TextRedactionResult(value=redacted, replacements=replacements, kinds=tuple(kinds))


def contains_secret_like(value: str) -> bool:
    return redact_secret_text(value).replacements > 0


def json_utf8_size(value: Any) -> int:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return len(serialized.encode("utf-8"))


def secure_observation_payload(
    payload: dict[str, Any],
    *,
    max_input_bytes: int = OBSERVATION_MAX_INPUT_BYTES,
    max_sanitized_bytes: int = OBSERVATION_MAX_SANITIZED_BYTES,
) -> dict[str, Any]:
    input_bytes = json_utf8_size(payload)
    if input_bytes > max_input_bytes:
        raise ObservationPayloadTooLarge(
            stage="input",
            actual_bytes=input_bytes,
            limit_bytes=max_input_bytes,
        )

    input_payload_state = str(payload.get("payloadState") or "raw")
    requested_sensitivity = str(payload.get("sensitivity") or "internal").casefold()
    sanitizer = PayloadSanitizer()
    sanitized = sanitizer.sanitize(payload)
    if not isinstance(sanitized, dict):
        sanitized = {"data": sanitized}

    sensitivity: ObservationSensitivity = "restricted" if sanitizer.redaction_count else "internal"
    if requested_sensitivity == "restricted":
        sensitivity = "restricted"

    sanitized["payloadState"] = "sanitized"
    sanitized["sensitivity"] = sensitivity
    metadata = sanitized.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    else:
        metadata = dict(metadata)
    metadata.pop("security", None)
    sanitized["metadata"] = metadata

    report = sanitizer.report(
        input_bytes=input_bytes,
        sanitized_bytes=0,
        input_payload_state=input_payload_state,
    )
    report["sensitivity"] = sensitivity
    metadata["security"] = report
    sanitized_bytes = 0
    for _ in range(4):
        next_size = json_utf8_size(sanitized)
        report["sanitizedBytes"] = next_size
        if next_size == sanitized_bytes:
            break
        sanitized_bytes = next_size
    sanitized_bytes = json_utf8_size(sanitized)
    report["sanitizedBytes"] = sanitized_bytes

    if sanitized_bytes > max_sanitized_bytes:
        raise ObservationPayloadTooLarge(
            stage="sanitized",
            actual_bytes=sanitized_bytes,
            limit_bytes=max_sanitized_bytes,
        )

    return sanitized


def observation_body_limit(path: str) -> int | None:
    if path == "/bhm/observe":
        return OBSERVATION_MAX_INPUT_BYTES
    if path == "/bhm/hooks/compact":
        return OBSERVATION_COMPACT_MAX_INPUT_BYTES
    if path == "/bhm/hooks/idle":
        return OBSERVATION_IDLE_MAX_INPUT_BYTES
    return None
