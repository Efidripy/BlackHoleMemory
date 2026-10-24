"""Shared contracts, configuration, errors and validators for governed semantic editor."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .governed_consolidation import MAX_BASIS
from .governed_consolidation import OPERATIONS
from .governed_consolidation import GovernedConsolidationError


GOVERNED_SEMANTIC_EDITOR_SCHEMA_VERSION = "bhm.governed-semantic-editor.v1"
GOVERNED_SEMANTIC_EDITOR_ANALYZER = "bhm-local-semantic-editor/v1"
GOVERNED_SEMANTIC_EDITOR_PROMPT_ID = "governed-semantic-editor"
GOVERNED_SEMANTIC_EDITOR_PROMPT_VERSION = "5"
# This version describes the deterministic receipt, independently of the
# model prompt.  Consumers must not treat an unversioned policy mapping as a
# current semantic-safety decision.
GOVERNED_SEMANTIC_EDITOR_POLICY_VERSION = "2"
GOVERNED_SEMANTIC_EVIDENCE_BINDING_SCHEMA_VERSION = "bhm.governed-semantic-evidence-binding.v1"
MAX_RETRIEVAL_CANDIDATES = 20
MIN_RETRIEVAL_CANDIDATES = 1
MAX_QUERY_CHARS = 480
MAX_CONFLICTS = 16
MIN_CREATE_CONFIDENCE = 0.72
# Workflow/checkpoint/session records are operational traces, while runbooks
# and architecture notes are instruction-bearing documents rather than atomic
# propositions to consolidate. Feeding any of them to a semantic editor turns
# historical instructions into model evidence. They remain searchable through
# their own BHM surfaces but are excluded here.
SEMANTIC_EDITOR_MEMORY_TYPES = frozenset({
    "audit",
    "bug",
    "crystal",
    "decision",
    "fact",
    "knowledge-crystal",
    "pattern",
})
# Keep the local 7B editor inside a practical foreground budget even when an
# operator requests all 20 candidates. SQLite keeps the complete canonical
# revisions; the model receives only a bounded analysis view. Six thousand
# characters leaves room for a schema-constrained reply within the 60-second
# default instead of making a healthy local provider look unavailable.
MAX_MODEL_EVIDENCE_CHARS = 6_000
MAX_MODEL_RECORD_CONTENT_CHARS = 1_800
DEFAULT_SEMANTIC_EDITOR_TIMEOUT_SECONDS = 60.0
DEFAULT_SEMANTIC_EDITOR_MAX_TOKENS = 180
MAX_SEMANTIC_EDITOR_CONTRACT_ATTEMPTS = 3
_SEMANTIC_EDITOR_NO_OP_EXAMPLE = (
    '{"operation":"no_op","candidate":{"title":"","content":"",'
    '"memory_type":"fact","concepts":[],"files":[]},"confidence":0.0,'
    '"conflicts":[],"reason":"Evidence is insufficient for a governed change."}'
)

# The local gateway still independently parses and validates this response.
# This only asks compatible local runners to constrain syntactic JSON so a
# malformed prose answer cannot consume the bounded foreground editor budget.
GOVERNED_SEMANTIC_EDITOR_JSON_SCHEMA: dict[str, Any] = {
    "name": "governed_semantic_proposal",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation", "candidate", "confidence", "conflicts", "reason"],
        "properties": {
            "operation": {"type": "string", "enum": sorted(OPERATIONS)},
            "candidate": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "content", "memory_type", "concepts", "files"],
                "properties": {
                    "title": {"type": "string", "maxLength": 240},
                    "content": {"type": "string", "maxLength": 12000},
                    "memory_type": {"type": "string", "maxLength": 96},
                    "concepts": {"type": "array", "maxItems": 24, "items": {"type": "string", "maxLength": 240}},
                    "files": {"type": "array", "maxItems": 24, "items": {"type": "string", "maxLength": 240}},
                    "target_memory_id": {"type": "string", "maxLength": 180},
                    "relation": {"type": "string", "maxLength": 96},
                    "evidence_refs": {"type": "array", "minItems": 1, "maxItems": MAX_BASIS, "items": {"type": "integer", "minimum": 1, "maximum": MAX_BASIS}},
                    "target_evidence_ref": {"type": "integer", "minimum": 1, "maximum": MAX_BASIS},
                },
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "conflicts": {"type": "array", "maxItems": MAX_CONFLICTS, "items": {"type": "string", "maxLength": 240}},
            "reason": {"type": "string", "minLength": 12, "maxLength": 480},
        },
    },
}


class GovernedSemanticEditorError(GovernedConsolidationError):
    """A semantic candidate is malformed or violates governance policy."""


class GovernedSemanticEditorUnavailable(GovernedSemanticEditorError):
    """The opt-in local semantic adapter cannot run in the current runtime."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "local_model_unavailable",
        diagnostic: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        # Never retain model output or source evidence in an error receipt.
        # The bounded values below are enough to distinguish a malformed
        # reply from a provider outage during operator diagnosis.
        self.diagnostic = _redacted_gateway_diagnostic(diagnostic)


class SemanticCompletion(Protocol):
    """Small testable boundary around a local-only structured completion."""

    def complete(self, *, project: str, query: str, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class SemanticEditorConfig:
    """Explicit local-only configuration; disabled is always the default."""

    enabled: bool
    base_url: str
    model_id: str
    timeout_seconds: float
    max_tokens: int

    @classmethod
    def from_env(cls) -> "SemanticEditorConfig":
        enabled = str(os.getenv("BHM_GOVERNED_SEMANTIC_EDITOR_ENABLED") or "").strip().casefold() in {
            "1", "true", "yes", "on"
        }
        try:
            # The governed editor returns a compact strict proposal, not prose.
            # Keep its default inference budget suitable for the supported local
            # Qwen 7B runtime: 900 tokens can outlive the foreground timeout and
            # turn a healthy provider into a misleading timeout fallback.
            timeout_seconds = min(
                max(float(os.getenv("BHM_GOVERNED_SEMANTIC_EDITOR_TIMEOUT_SECONDS", str(DEFAULT_SEMANTIC_EDITOR_TIMEOUT_SECONDS))), 1.0),
                120.0,
            )
            max_tokens = min(
                max(int(os.getenv("BHM_GOVERNED_SEMANTIC_EDITOR_MAX_TOKENS", str(DEFAULT_SEMANTIC_EDITOR_MAX_TOKENS))), 64),
                2048,
            )
        except ValueError as exc:
            raise GovernedSemanticEditorUnavailable(
                "invalid local semantic editor configuration",
                code="semantic_editor_invalid_configuration",
            ) from exc
        return cls(
            enabled=enabled,
            base_url=str(os.getenv("BHM_GOVERNED_SEMANTIC_EDITOR_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "http://127.0.0.1:1234/v1").strip(),
            model_id=str(os.getenv("BHM_GOVERNED_SEMANTIC_EDITOR_MODEL") or os.getenv("OPENAI_MODEL") or "qwen2.5-coder-7b-instruct").strip(),
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )


def _redacted_gateway_diagnostic(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return bounded contract telemetry without model output or evidence."""

    source = dict(value or {})
    missing = source.get("missing_keys")
    return {
        "response_chars": min(max(int(source.get("response_chars") or 0), 0), 100_000),
        "parsed_json": bool(source.get("parsed_json")),
        "validation_checked": bool(source.get("validation_checked")),
        "missing_keys": [str(item)[:64] for item in missing[:16]] if isinstance(missing, list) else [],
        "semantic_error": str(source.get("semantic_error") or "")[:160],
    }


def _gateway_result_diagnostic(result: Any) -> dict[str, Any]:
    """Extract bounded diagnostics without retaining model output or evidence."""

    return {
        "response_chars": len(str(getattr(result, "content", "") or "")),
        "parsed_json": isinstance(getattr(result, "parsed_json", None), Mapping),
        "validation_checked": bool((getattr(result, "validation", {}) or {}).get("checked")),
        "missing_keys": list((getattr(result, "validation", {}) or {}).get("missing_keys") or ()),
    }


def _validate_model_output_shape(output: Mapping[str, Any]) -> None:
    """Reject malformed model semantics before a proposal reaches the API."""

    operation = _required_text(output.get("operation"), "operation", 32).casefold()
    if operation not in OPERATIONS:
        raise GovernedSemanticEditorError("unsupported semantic editor operation")
    raw_candidate = output.get("candidate")
    if not isinstance(raw_candidate, Mapping):
        raise GovernedSemanticEditorError("candidate must be an object")
    # Model-facing evidence intentionally contains no opaque canonical IDs.
    # Discarding a hallucinated target here lets ``build_proposal`` bind every
    # target operation to the deterministic same-project SQLite basis below;
    # passing it through would reject a safe link/revise/supersede merely
    # because a model invented an identifier.
    candidate = {key: value for key, value in raw_candidate.items() if key != "target_memory_id"}
    for field, limit in (("title", 240), ("content", 12000), ("memory_type", 96)):
        value = candidate.get(field)
        if not isinstance(value, str) or len(value) > limit:
            raise GovernedSemanticEditorError(f"candidate {field} must be bounded text")
    _string_list(candidate.get("concepts"), "candidate concepts", 24)
    _string_list(candidate.get("files"), "candidate files", 24)
    if operation in {"create", "revise", "supersede"}:
        _required_text(candidate.get("title"), "candidate title", 240)
        _required_text(candidate.get("content"), "candidate content", 12000)
    if operation == "link":
        _required_text(candidate.get("relation"), "candidate relation", 96)
    evidence_refs = candidate.get("evidence_refs")
    if evidence_refs is not None:
        if not isinstance(evidence_refs, list) or len(evidence_refs) > MAX_BASIS:
            raise GovernedSemanticEditorError("candidate evidence_refs must be a bounded array")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in evidence_refs):
            raise GovernedSemanticEditorError("candidate evidence_refs must contain positions")
    target_ref = candidate.get("target_evidence_ref")
    if target_ref is not None and (isinstance(target_ref, bool) or not isinstance(target_ref, int)):
        raise GovernedSemanticEditorError("candidate target_evidence_ref must be a position")
    _confidence(output.get("confidence"))
    _string_list(output.get("conflicts"), "conflicts", MAX_CONFLICTS)
    _required_text(output.get("reason"), "reason", 480)


def clamp_retrieval_limit(value: int) -> int:
    """Keep semantic retrieval in the documented 1..20 evidence budget."""

    return min(max(int(value), MIN_RETRIEVAL_CANDIDATES), MAX_RETRIEVAL_CANDIDATES)


def _assert_same_project_records(project: str, records: Sequence[Mapping[str, Any]]) -> None:
    if not records:
        raise GovernedSemanticEditorError("semantic editor requires at least one canonical record")
    if len(records) > MAX_RETRIEVAL_CANDIDATES:
        raise GovernedSemanticEditorError("semantic editor record limit exceeded")
    ids: set[str] = set()
    for record in records:
        memory_id = _required_text(record.get("id") or record.get("source_id"), "record memory id", 180)
        if memory_id in ids:
            raise GovernedSemanticEditorError("semantic editor records must be unique")
        ids.add(memory_id)
        if str(record.get("project") or "") != project:
            raise GovernedSemanticEditorError("semantic editor cross-project evidence is forbidden")


def _required_text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise GovernedSemanticEditorError(f"{field} is required")
    if len(text) > limit:
        raise GovernedSemanticEditorError(f"{field} exceeds {limit} characters")
    return text


def _string_list(value: Any, field: str, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise GovernedSemanticEditorError(f"{field} must be an array")
    result: list[str] = []
    for item in value:
        normalized = _required_text(item, field, 240)
        if normalized not in result:
            result.append(normalized)
        if len(result) > limit:
            raise GovernedSemanticEditorError(f"{field} exceeds {limit} items")
    return result


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise GovernedSemanticEditorError("confidence must be numeric") from exc
    if not 0.0 <= parsed <= 1.0:
        raise GovernedSemanticEditorError("confidence must be within 0..1")
    return round(parsed, 6)


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
