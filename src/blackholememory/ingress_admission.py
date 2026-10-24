"""Deterministic, non-persistent admission for new BHM content.

This module deliberately answers only whether a caller may enter a given
store. It neither stores quarantine payloads nor calls a model, network,
SQLite, Mem0, Qdrant, graph, or filesystem API. A rejected value therefore
has no retrieval or projection path. SQLite remains the sole authority for
admitted memory; projections remain rebuildable derivatives.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from .llm_safety import scan_prompt_injection
from .observation_security import contains_secret_like


INGRESS_ADMISSION_SCHEMA_VERSION = "bhm.ingress-admission.v1"


class IngressKind(StrEnum):
    """Server-selected source class; clients cannot provide a trust label."""

    OPERATOR = "operator"
    PROMPT = "prompt"
    TOOL = "tool"
    WEB = "web"
    MCP = "mcp"
    IMPORT = "import"
    OBSERVATION = "observation"


@dataclass(frozen=True)
class IngressAdmission:
    ingress: IngressKind
    project: str
    actor: str
    decision: str
    authority_eligible: bool
    reason_codes: tuple[str, ...]
    content_sha256: str
    provenance_sha256: str

    @property
    def admitted(self) -> bool:
        return self.decision == "allow"

    def receipt(self) -> dict[str, Any]:
        """Return a digest-only, replay-stable receipt safe for API errors."""

        return {
            "schema_version": INGRESS_ADMISSION_SCHEMA_VERSION,
            "ingress": self.ingress.value,
            "project": self.project,
            "actor": self.actor,
            "decision": self.decision,
            "authority_eligible": self.authority_eligible,
            "reason_codes": list(self.reason_codes),
            "content_sha256": self.content_sha256,
            "provenance_sha256": self.provenance_sha256,
            "raw_emitted": False,
            "quarantine_persisted": False,
            "projection_eligible": False,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _flatten(value: Any, *, limit: int = 16_384) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, Mapping):
        return " ".join(f"{key}:{_flatten(item, limit=limit)}" for key, item in sorted(value.items(), key=lambda pair: str(pair[0])))[:limit]
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(item, limit=limit) for item in list(value)[:128])[:limit]
    return str(value)[:limit]


def _client_trust_claims(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().casefold()
            if normalized == "trust_label" and str(item).strip().casefold() in {"authoritative", "reviewed"}:
                return True
            if normalized in {"authoritative", "reviewed"} and item is True:
                return True
            if normalized == "reviewer" and str(item).strip():
                return True
            if _client_trust_claims(item):
                return True
    elif isinstance(value, (list, tuple, set)):
        return any(_client_trust_claims(item) for item in value)
    return False


def _foreign_project(value: Any, project: str) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).strip().casefold() in {"project", "project_id"} and item is not None and str(item).strip() and str(item).strip() != project:
                return True
            if _foreign_project(item, project):
                return True
    elif isinstance(value, (list, tuple, set)):
        return any(_foreign_project(item, project) for item in value)
    return False


def admit_ingress(*, ingress: IngressKind, project: str, actor: str, payload: Any) -> IngressAdmission:
    """Classify one ingress before any durable operation.

    ``operator`` is the only content ingress that may enter memory authority.
    MCP/tool/prompt/web/import values are intentionally non-authoritative; they
    are rejected rather than stored as retrievable quarantine. Observation is
    admitted only to its isolated observation journal.
    """

    normalized_project = str(project).strip()
    normalized_actor = str(actor).strip() or "unknown"
    canonical = _canonical_json(payload)
    text = _flatten(payload)
    reasons: list[str] = []
    if not normalized_project:
        reasons.append("project_missing")
    if _foreign_project(payload, normalized_project):
        reasons.append("cross_project")
    if _client_trust_claims(payload):
        reasons.append("client_trust_claim_rejected")
    if scan_prompt_injection(text):
        reasons.append("prompt_injection")
    # Observation/hook payloads have already passed the canonical recursive
    # redactor before this admission. Scanning its literal `[REDACTED:*]`
    # placeholders would reject a safe journal event and defeat the existing
    # no-raw-secret contract. All other ingress is scanned before any write.
    if ingress is not IngressKind.OBSERVATION and contains_secret_like(text):
        reasons.append("secret_like_input")
    if ingress in {IngressKind.PROMPT, IngressKind.TOOL, IngressKind.WEB, IngressKind.MCP, IngressKind.IMPORT}:
        reasons.append("non_authoritative_ingress")

    reason_codes = tuple(dict.fromkeys(reasons))
    decision = "allow" if not reason_codes else "reject"
    return IngressAdmission(
        ingress=ingress,
        project=normalized_project,
        actor=normalized_actor,
        decision=decision,
        authority_eligible=decision == "allow" and ingress is IngressKind.OPERATOR,
        reason_codes=reason_codes,
        content_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        provenance_sha256=hashlib.sha256(
            _canonical_json({"ingress": ingress.value, "project": normalized_project, "actor": normalized_actor}).encode("utf-8")
        ).hexdigest(),
    )
