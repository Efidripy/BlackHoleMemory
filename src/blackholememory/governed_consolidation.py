"""Governed, project-scoped memory consolidation without a second authority.

The upstream ``mem0ai`` writer performs vector-store mutations when it infers
add/update/delete actions.  This module intentionally does not import Mem0.
It offers a deterministic, bounded proposal analyzer and a narrow persistence
adapter whose canonical apply is delegated to :class:`SQLiteMemoryRepository`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .domain import Lifecycle, Memory, MemoryLink, MemoryRevision, Provenance, content_sha256
from .filesystem_boundaries import assert_safe_path
from .memory_contracts import MemoryClass, MemoryClassSource, MemoryEventRole
from .memory_repository import MemoryRepositoryIntegrityError, MemoryRevisionConflict, SQLiteMemoryRepository


GOVERNED_CONSOLIDATION_SCHEMA_VERSION = "bhm.governed-consolidation.v1"
GOVERNED_CONSOLIDATION_CAPABILITY_KEY = "governed_consolidation_schema"
GOVERNED_CONSOLIDATION_CAPABILITY_VERSION = "1"
GOVERNED_CONSOLIDATION_ANALYZER = "bhm-native-deterministic/v1"
MAX_BASIS = 32
MAX_PROPOSALS = 200
MAX_CONTENT_CHARS = 8_000
OPERATIONS = frozenset({"no_op", "create", "revise", "supersede", "archive", "link"})
STATUSES = frozenset({"proposed", "approved", "rejected", "applied", "stale", "failed"})
SEMANTIC_EDITOR_ANALYZER_PREFIX = "bhm-local-semantic-editor/"
SEMANTIC_EVIDENCE_BINDING_SCHEMA_VERSION = "bhm.governed-semantic-evidence-binding.v1"
SEMANTIC_OPERATOR_ATTESTATION_SCHEMA_VERSION = "bhm.governed-semantic-operator-attestation.v2"
SEMANTIC_REVIEW_SCHEMA_VERSION = "bhm.governed-semantic-review.v1"


class GovernedConsolidationError(RuntimeError):
    """Base error for proposal and apply policy failures."""


class GovernedConsolidationDisabled(GovernedConsolidationError):
    """Raised when the explicit runtime feature flag is not enabled."""


class GovernedConsolidationMigrationRequired(GovernedConsolidationError):
    """Raised when an operator has not applied the additive schema migration."""


class GovernedConsolidationApprovalRequired(GovernedConsolidationError):
    """Raised when apply lacks proposal-specific human approval."""


class GovernedConsolidationStale(GovernedConsolidationError):
    """Raised when canonical basis evidence drifted after proposal creation."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _elapsed_ms(started_at: float) -> float:
    """Return a content-free proposal duration with a stable representation."""

    return round(max(0.0, (time.perf_counter() - started_at) * 1_000.0), 3)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def canonical_json_sha256(value: Any) -> str:
    """Hash one canonical JSON value for an immutable proposal binding."""

    return _sha256(_canonical_json(value))


def _bounded_text(value: Any, *, limit: int, field: str, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise GovernedConsolidationError(f"{field} is required")
    if len(text) > limit:
        raise GovernedConsolidationError(f"{field} exceeds {limit} characters")
    return text


def _bounded_items(value: Any, *, limit: int, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise GovernedConsolidationError(f"{field} must be an array")
    result: list[str] = []
    for item in value:
        normalized = _bounded_text(item, limit=240, field=field)
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def runtime_enabled() -> bool:
    """Return the explicit, default-off runtime gate."""

    return str(os.getenv("BHM_GOVERNED_CONSOLIDATION_ENABLED") or "").strip().casefold() in {"1", "true", "yes", "on"}


def runtime_status(database_path: Path | str) -> dict[str, Any]:
    """Content-safe status suitable for health and operator diagnostics."""

    if not runtime_enabled():
        return {
            "state": "disabled",
            "enabled": False,
            "mode": "proposal-only",
            "analyzer": GOVERNED_CONSOLIDATION_ANALYZER,
            "direct_mem0_writes": False,
            "direct_qdrant_writes": False,
        }
    try:
        repository = GovernedConsolidationRepository(database_path)
        counts = repository.count_by_status()
    except GovernedConsolidationMigrationRequired:
        return {
            "state": "degraded",
            "enabled": True,
            "reason": "schema_migration_required",
            "mode": "proposal-only",
            "analyzer": GOVERNED_CONSOLIDATION_ANALYZER,
            "direct_mem0_writes": False,
            "direct_qdrant_writes": False,
        }
    return {
        "state": "approval-gated",
        "enabled": True,
        "mode": "approval-gated",
        "analyzer": GOVERNED_CONSOLIDATION_ANALYZER,
        "counts": counts,
        "direct_mem0_writes": False,
        "direct_qdrant_writes": False,
    }


def _record_basis(record: Mapping[str, Any], project: str) -> dict[str, str]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    memory_id = _bounded_text(record.get("id") or record.get("source_id"), limit=180, field="basis.memory_id", required=True)
    record_project = _bounded_text(record.get("project"), limit=160, field="basis.project", required=True)
    if record_project != project:
        raise GovernedConsolidationError("cross-project basis is forbidden")
    revision_id = _bounded_text(metadata.get("revision_id") or record.get("revision_id"), limit=180, field="basis.revision_id", required=True)
    digest = _bounded_text(
        metadata.get("content_sha256") or record.get("content_sha256") or content_sha256(str(record.get("content") or "")),
        limit=64,
        field="basis.content_sha256",
        required=True,
    ).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise GovernedConsolidationError("basis.content_sha256 must be SHA-256")
    return {"memory_id": memory_id, "revision_id": revision_id, "content_sha256": digest}


def _candidate_payload(candidate: Mapping[str, Any], *, project: str) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise GovernedConsolidationError("candidate must be an object")
    content = _bounded_text(candidate.get("content"), limit=MAX_CONTENT_CHARS, field="candidate.content")
    title = _bounded_text(candidate.get("title"), limit=240, field="candidate.title")
    memory_type = _bounded_text(candidate.get("memory_type") or "fact", limit=96, field="candidate.memory_type", required=True)
    normalized = {
        "title": title,
        "content": content,
        "memory_type": memory_type,
        "concepts": _bounded_items(candidate.get("concepts"), limit=24, field="candidate.concepts"),
        "files": _bounded_items(candidate.get("files"), limit=24, field="candidate.files"),
    }
    target_memory_id = _bounded_text(candidate.get("target_memory_id"), limit=180, field="candidate.target_memory_id")
    if target_memory_id:
        normalized["target_memory_id"] = target_memory_id
    relation = _bounded_text(candidate.get("relation"), limit=96, field="candidate.relation")
    if relation:
        normalized["relation"] = relation
    retire_basis_memory_ids = _bounded_items(
        candidate.get("retire_basis_memory_ids"),
        limit=MAX_BASIS,
        field="candidate.retire_basis_memory_ids",
    )
    if retire_basis_memory_ids:
        normalized["retire_basis_memory_ids"] = retire_basis_memory_ids
    return normalized


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise GovernedConsolidationError("confidence must be numeric") from exc
    if not 0.0 <= parsed <= 1.0:
        raise GovernedConsolidationError("confidence must be within 0..1")
    return round(parsed, 6)


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _semantic_binding_error(message: str) -> None:
    raise GovernedConsolidationError(f"semantic evidence binding invalid: {message}")


def _is_semantic_proposal(proposal: Mapping[str, Any]) -> bool:
    return str(proposal.get("analyzer") or "").startswith(SEMANTIC_EDITOR_ANALYZER_PREFIX)


def _semantic_attestation_error(message: str) -> None:
    raise GovernedConsolidationApprovalRequired(f"semantic operator attestation invalid: {message}")


def _semantic_attestation_targets(binding: Mapping[str, Any]) -> list[str]:
    """Return the exact, ordered lifecycle endpoints that an operator reviewed."""

    targets: list[str] = []
    for key in ("source", "target"):
        item = binding.get(key)
        if isinstance(item, Mapping):
            memory_id = str(item.get("memory_id") or "")
            if not memory_id:
                _semantic_attestation_error(f"binding {key} has no memory id")
            targets.append(memory_id)
    return targets


def _semantic_attestation_support_ids(binding: Mapping[str, Any]) -> list[str]:
    """Return the exact support rows whose meaning the reviewer assessed."""

    support = binding.get("support")
    if not isinstance(support, list) or not support:
        _semantic_attestation_error("binding support is required")
    identifiers: list[str] = []
    for item in support:
        if not isinstance(item, Mapping):
            _semantic_attestation_error("binding support entry is malformed")
        memory_id = str(item.get("memory_id") or "")
        if not memory_id:
            _semantic_attestation_error("binding support entry has no memory id")
        identifiers.append(memory_id)
    if len(set(identifiers)) != len(identifiers):
        _semantic_attestation_error("binding support memory ids must be distinct")
    return identifiers


def _validate_semantic_review(review: Any, *, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Require an exact, content-free human semantic verdict before approval.

    The model and structural identity binding cannot prove entailment.  This
    compact verdict makes the privileged reviewer explicitly attest that every
    bound support row was relevant, the candidate was entailed, and no hidden
    conflict was observed.  It is bound to the immutable support identities;
    it does not claim an automatic semantic proof and never enables auto-apply.
    """

    if not isinstance(review, Mapping):
        _semantic_attestation_error("semantic review is required")
    normalized = dict(review)
    expected_keys = {
        "schema_version",
        "reviewed_support_memory_ids",
        "candidate_entailment",
        "relevance_assessment",
        "hidden_conflict_assessment",
    }
    if set(normalized) != expected_keys:
        _semantic_attestation_error("semantic review keys are unsupported")
    if normalized.get("schema_version") != SEMANTIC_REVIEW_SCHEMA_VERSION:
        _semantic_attestation_error("semantic review schema is unsupported")
    support_ids = normalized.get("reviewed_support_memory_ids")
    if not isinstance(support_ids, list) or any(not isinstance(item, str) or not item for item in support_ids):
        _semantic_attestation_error("reviewed support memory ids must be a string array")
    if support_ids != _semantic_attestation_support_ids(binding):
        _semantic_attestation_error("reviewed support memory ids do not match binding")
    if normalized.get("candidate_entailment") != "supported":
        _semantic_attestation_error("candidate entailment must be supported")
    if normalized.get("relevance_assessment") != "all_support_relevant":
        _semantic_attestation_error("all support must be relevant")
    if normalized.get("hidden_conflict_assessment") != "none_observed":
        _semantic_attestation_error("hidden conflict assessment must be none_observed")
    return normalized


def validate_semantic_operator_attestation(
    attestation: Any,
    *,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a privileged semantic approval without persisting reviewer text.

    The attestation deliberately repeats the identity fields an operator sees.
    It is stored only inside the immutable approval-event JSON, so no SQLite
    schema change or broadening of non-semantic decisions is required.
    """

    if not _is_semantic_proposal(proposal):
        _semantic_attestation_error("proposal is not semantic")
    if not isinstance(attestation, Mapping):
        _semantic_attestation_error("attestation is required")
    normalized = dict(attestation)
    expected_keys = {
        "schema_version",
        "proposal_id",
        "proposal_digest",
        "identity_binding",
        "model_operation",
        "effective_operation",
        "target_memory_ids",
        "semantic_review",
        "reviewer_outcome",
    }
    if set(normalized) != expected_keys:
        _semantic_attestation_error("attestation keys are unsupported")
    if normalized.get("schema_version") != SEMANTIC_OPERATOR_ATTESTATION_SCHEMA_VERSION:
        _semantic_attestation_error("attestation schema is unsupported")
    if normalized.get("proposal_id") != proposal.get("proposal_id"):
        _semantic_attestation_error("proposal id does not match")
    if normalized.get("proposal_digest") != proposal.get("proposal_digest"):
        _semantic_attestation_error("proposal digest does not match")
    binding = proposal.get("identity_binding")
    if not isinstance(binding, Mapping) or normalized.get("identity_binding") != dict(binding):
        _semantic_attestation_error("identity binding does not match")
    normalized["semantic_review"] = _validate_semantic_review(
        normalized.get("semantic_review"),
        binding=dict(binding),
    )
    semantic = proposal.get("semantic_editor")
    policy = semantic.get("policy") if isinstance(semantic, Mapping) and isinstance(semantic.get("policy"), Mapping) else None
    if policy is None:
        _semantic_attestation_error("policy receipt is required")
    if normalized.get("model_operation") != policy.get("model_operation"):
        _semantic_attestation_error("model operation does not match policy receipt")
    if normalized.get("effective_operation") != proposal.get("operation") or normalized.get("effective_operation") != policy.get("effective_operation"):
        _semantic_attestation_error("effective operation does not match")
    raw_targets = normalized.get("target_memory_ids")
    if not isinstance(raw_targets, list) or any(not isinstance(item, str) or not item for item in raw_targets):
        _semantic_attestation_error("target memory ids must be a string array")
    if raw_targets != _semantic_attestation_targets(dict(binding)):
        _semantic_attestation_error("target memory ids do not match binding")
    if normalized.get("reviewer_outcome") != "approved":
        _semantic_attestation_error("reviewer outcome must be approved")
    return normalized


def _validate_identity_binding(
    binding: Any,
    *,
    basis: list[dict[str, str]],
    candidate: Mapping[str, Any],
    operation: str,
) -> dict[str, Any]:
    """Validate the evidence/target receipt that is included in proposal identity.

    This is deliberately structural: it validates exact canonical IDs/revisions,
    content hashes and target endpoints without pretending that lexical variation
    proves semantic entailment.  A human reviewer still reads the support rows.
    """

    if not isinstance(binding, Mapping):
        _semantic_binding_error("binding is required")
    normalized = dict(binding)
    expected_keys = {"schema_version", "policy_sha256", "candidate_sha256", "support", "target", "source"}
    if set(normalized) != expected_keys:
        _semantic_binding_error("binding keys are unsupported")
    if normalized.get("schema_version") != SEMANTIC_EVIDENCE_BINDING_SCHEMA_VERSION:
        _semantic_binding_error("binding schema is unsupported")
    if not _is_sha256(normalized.get("policy_sha256")) or not _is_sha256(normalized.get("candidate_sha256")):
        _semantic_binding_error("binding digests must be SHA-256")
    if normalized["candidate_sha256"] != canonical_json_sha256(dict(candidate)):
        _semantic_binding_error("candidate digest does not match")
    support = normalized.get("support")
    if not isinstance(support, list) or support != basis:
        _semantic_binding_error("support must exactly match canonical basis")
    target = normalized.get("target")
    source = normalized.get("source")
    lifecycle = {"revise", "supersede", "archive", "link"}
    if operation in lifecycle:
        if not isinstance(target, Mapping) or dict(target) not in basis:
            _semantic_binding_error("exact lifecycle target is required")
        if candidate.get("target_memory_id") != target.get("memory_id"):
            _semantic_binding_error("candidate target does not match binding")
    elif target is not None:
        _semantic_binding_error("non-lifecycle operation cannot bind a target")
    if operation == "link":
        if len(basis) != 2 or not isinstance(source, Mapping) or dict(source) not in basis:
            _semantic_binding_error("link requires exactly two bound endpoints")
        if source.get("memory_id") == target.get("memory_id"):
            _semantic_binding_error("link endpoints must differ")
    elif source is not None:
        _semantic_binding_error("only link may bind a source")
    return normalized


def _proposal_identity(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the immutable fields covered by a proposal digest."""

    keys = (
        "schema_version", "project", "operation", "basis", "candidate", "reason",
        "confidence", "conflicts", "analyzer", "model_or_provider",
    )
    if any(key not in proposal for key in keys):
        raise GovernedConsolidationError("proposal identity is incomplete")
    identity = {key: proposal[key] for key in keys}
    if "identity_binding" in proposal:
        identity["identity_binding"] = proposal["identity_binding"]
    return identity


def validate_proposal_identity(proposal: Mapping[str, Any]) -> None:
    """Fail closed when the exact persisted proposal identity was altered."""

    identity = _proposal_identity(proposal)
    expected_digest = canonical_json_sha256(identity)
    actual_digest = str(proposal.get("proposal_digest") or "")
    if actual_digest != expected_digest or str(proposal.get("proposal_id") or "") != f"gcp_bhm_{expected_digest[:24]}":
        raise GovernedConsolidationError("proposal identity digest does not match")
    analyzer = str(identity["analyzer"] or "")
    if analyzer.startswith(SEMANTIC_EDITOR_ANALYZER_PREFIX):
        binding = _validate_identity_binding(
            identity.get("identity_binding"),
            basis=list(identity["basis"]),
            candidate=dict(identity["candidate"]),
            operation=str(identity["operation"]),
        )
        semantic = proposal.get("semantic_editor")
        if not isinstance(semantic, Mapping) or semantic.get("binding") != binding:
            _semantic_binding_error("receipt binding does not match identity")
        policy = semantic.get("policy")
        if not isinstance(policy, Mapping) or canonical_json_sha256(dict(policy)) != binding["policy_sha256"]:
            _semantic_binding_error("policy receipt digest does not match")
        if semantic.get("proposal_identity_digest") != expected_digest:
            _semantic_binding_error("receipt does not bind proposal identity")


def build_proposal(
    *,
    project: str,
    records: Iterable[Mapping[str, Any]],
    operation: str,
    candidate: Mapping[str, Any],
    reason: str,
    confidence: float = 0.75,
    conflicts: Iterable[str] = (),
    analyzer: str = GOVERNED_CONSOLIDATION_ANALYZER,
    model_or_provider: str | None = None,
    analysis_duration_ms: float | None = None,
    identity_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one deterministic, non-persisting project-scoped proposal."""

    canonical_project = _bounded_text(project, limit=160, field="project", required=True)
    normalized_operation = _bounded_text(operation, limit=32, field="operation", required=True).casefold()
    if normalized_operation not in OPERATIONS:
        raise GovernedConsolidationError("unsupported consolidation operation")
    basis = sorted({_canonical_json(_record_basis(record, canonical_project)) for record in records})
    if not basis or len(basis) > MAX_BASIS:
        raise GovernedConsolidationError(f"basis must contain 1..{MAX_BASIS} records")
    decoded_basis = [json.loads(item) for item in basis]
    normalized_candidate = _candidate_payload(candidate, project=canonical_project)
    if normalized_operation in {"create", "revise", "supersede"} and not normalized_candidate["content"]:
        raise GovernedConsolidationError("content is required for the selected operation")
    if normalized_operation in {"revise", "supersede", "archive", "link"}:
        target_id = normalized_candidate.get("target_memory_id") or decoded_basis[0]["memory_id"]
        if target_id not in {item["memory_id"] for item in decoded_basis}:
            raise GovernedConsolidationError("target memory must be in same-project basis")
        normalized_candidate["target_memory_id"] = target_id
    if normalized_operation == "link" and not normalized_candidate.get("relation"):
        raise GovernedConsolidationError("link operation requires candidate.relation")
    normalized_reason = _bounded_text(reason, limit=480, field="reason", required=True)
    normalized_conflicts = _bounded_items(list(conflicts), limit=16, field="conflicts")
    identity = {
        "schema_version": GOVERNED_CONSOLIDATION_SCHEMA_VERSION,
        "project": canonical_project,
        "operation": normalized_operation,
        "basis": decoded_basis,
        "candidate": normalized_candidate,
        "reason": normalized_reason,
        "confidence": _confidence(confidence),
        "conflicts": normalized_conflicts,
        "analyzer": _bounded_text(analyzer, limit=160, field="analyzer", required=True),
        "model_or_provider": _bounded_text(model_or_provider, limit=160, field="model_or_provider") or None,
    }
    if identity_binding is not None:
        identity["identity_binding"] = _validate_identity_binding(
            identity_binding,
            basis=decoded_basis,
            candidate=normalized_candidate,
            operation=normalized_operation,
        )
    proposal_digest = canonical_json_sha256(identity)
    return {
        "proposal_id": f"gcp_bhm_{proposal_digest[:24]}",
        "proposal_digest": proposal_digest,
        "status": "proposed",
        **identity,
        "provenance": {
            "analyzer": identity["analyzer"],
            "model_or_provider": identity["model_or_provider"],
            "created_at": _utc_now_iso(),
        },
        # This is deliberately content-free: candidates are the bounded
        # authority records examined by the analyzer, not model samples.
        "observability": {
            "candidate_count": len(decoded_basis),
            "analysis_duration_ms": round(max(0.0, float(analysis_duration_ms or 0.0)), 3),
        },
        "requires_human_approval": True,
        "execution": {
            "proposal_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "automatic_apply": False,
        },
    }


def analyze_records(
    *, project: str, records: Iterable[Mapping[str, Any]], operation: str = "create"
) -> dict[str, Any]:
    """Create a deterministic typed proposal from bounded local evidence.

    This deliberately small analyzer is an offline-safe baseline.  It does
    not call an LLM, network service, Mem0 client or embedding provider.
    """

    started_at = time.perf_counter()
    source = list(records)
    if len(source) > MAX_BASIS:
        raise GovernedConsolidationError(f"records exceed {MAX_BASIS}")
    contents = [str(item.get("content") or item.get("memory") or "").strip() for item in source]
    combined = " ".join(contents).casefold()
    installer_terms = ("uninstall", "project-scoped", "install-state", "host-wide", "owner review")
    if all(term in combined for term in installer_terms):
        candidate = {
            "title": "Canonical uninstall safety contract",
            "content": "Normal uninstall is project-scoped, interactive, requires a valid install-state log, and never performs host-wide cleanup by default. Legacy/orphan recovery is explicit manual-only and owner-reviewed.",
            "memory_type": "decision",
            "concepts": ["uninstall", "safety", "operator-review"],
            "files": [],
        }
        reason = "deterministic installer safety clauses agree across same-project basis"
        score = 0.92
    else:
        nonempty = [item for item in contents if item]
        if not nonempty:
            candidate = {"title": "", "content": "", "memory_type": "fact", "concepts": [], "files": []}
            return build_proposal(
                project=project,
                records=source,
                operation="no_op",
                candidate=candidate,
                reason="basis has no analyzable content",
                confidence=0.0,
                analysis_duration_ms=_elapsed_ms(started_at),
            )
        candidate = {
            "title": str(source[0].get("title") or "Consolidated project fact")[:240],
            "content": max(nonempty, key=lambda item: (len(item), item))[:MAX_CONTENT_CHARS],
            "memory_type": str(source[0].get("memory_type") or source[0].get("type") or "fact")[:96],
            "concepts": [],
            "files": [],
        }
        reason = "deterministic bounded baseline selected the most complete same-project fact"
        score = 0.55
    return build_proposal(
        project=project,
        records=source,
        operation=operation,
        candidate=candidate,
        reason=reason,
        confidence=score,
        analysis_duration_ms=_elapsed_ms(started_at),
    )


@dataclass(frozen=True)
class GovernedApplyResult:
    proposal_id: str
    status: str
    outbox_event_ids: tuple[str, ...]
    memory_ids: tuple[str, ...]
    link_id: str | None
    apply_duration_ms: float
    outbox: dict[str, Any]


class GovernedConsolidationRepository:
    """SQLite authority adapter for proposal state, never canonical memories."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        assert_safe_path(self.path)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_ready(self, connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute(
                "SELECT value FROM memory_store_meta WHERE key = ?",
                (GOVERNED_CONSOLIDATION_CAPABILITY_KEY,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise GovernedConsolidationMigrationRequired("governed consolidation schema is not installed") from exc
        if row is None or str(row["value"]) != GOVERNED_CONSOLIDATION_CAPABILITY_VERSION:
            raise GovernedConsolidationMigrationRequired("governed consolidation schema migration is required")

    def _write(self):
        class _Transaction:
            def __init__(self, outer: "GovernedConsolidationRepository") -> None:
                self.outer = outer
                self.connection: sqlite3.Connection | None = None

            def __enter__(self) -> sqlite3.Connection:
                self.connection = self.outer._connect()
                self.outer._ensure_ready(self.connection)
                self.connection.execute("BEGIN IMMEDIATE")
                return self.connection

            def __exit__(self, exc_type, exc, _tb) -> bool:
                assert self.connection is not None
                if exc_type is None:
                    self.connection.commit()
                else:
                    self.connection.rollback()
                self.connection.close()
                return False

        return _Transaction(self)

    def _read(self) -> sqlite3.Connection:
        connection = self._connect()
        self._ensure_ready(connection)
        return connection

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> dict[str, Any]:
        proposal = json.loads(str(row["proposal_json"]))
        proposal["status"] = str(row["status"])
        proposal["approval"] = {
            "approved_at": row["approved_at"],
            "approved_by_digest": row["approved_by_digest"],
            "rejected_at": row["rejected_at"],
            "rejected_by_digest": row["rejected_by_digest"],
            "applied_at": row["applied_at"],
            "stale_at": row["stale_at"],
            "failure_code": row["failure_code"],
        }
        return proposal

    def create(self, proposal: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        normalized = copy.deepcopy(dict(proposal))
        if normalized.get("status") != "proposed" or normalized.get("requires_human_approval") is not True:
            raise GovernedConsolidationError("only a proposed human-approved contract can be stored")
        validate_proposal_identity(normalized)
        proposal_id = _bounded_text(normalized.get("proposal_id"), limit=96, field="proposal_id", required=True)
        proposal_digest = _bounded_text(normalized.get("proposal_digest"), limit=64, field="proposal_digest", required=True)
        project = _bounded_text(normalized.get("project"), limit=160, field="project", required=True)
        if len(proposal_digest) != 64:
            raise GovernedConsolidationError("proposal_digest must be SHA-256")
        proposal_json = _canonical_json(normalized)
        now = _utc_now_iso()
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM governed_consolidation_proposals WHERE project = ? AND proposal_digest = ?",
                (project, proposal_digest),
            ).fetchone()
            if existing is not None:
                return self._row_to_proposal(existing), False
            connection.execute(
                """
                INSERT INTO governed_consolidation_proposals(
                    proposal_id, proposal_digest, project, operation, status, basis_json,
                    candidate_json, reason, confidence, conflicts_json, provenance_json,
                    proposal_json, requires_human_approval, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    proposal_id, proposal_digest, project, normalized["operation"],
                    _canonical_json(normalized["basis"]), _canonical_json(normalized["candidate"]),
                    normalized["reason"], normalized["confidence"], _canonical_json(normalized["conflicts"]),
                    _canonical_json(normalized["provenance"]), proposal_json, now, now,
                ),
            )
            self._append_event(connection, proposal_id, "created", now, {"proposal_digest": proposal_digest})
        return self.get(proposal_id, project=project), True

    def get(self, proposal_id: str, *, project: str | None = None) -> dict[str, Any]:
        connection = self._read()
        try:
            where = "proposal_id = ?" + (" AND project = ?" if project else "")
            params: tuple[Any, ...] = (proposal_id,) + ((project,) if project else ())
            row = connection.execute(f"SELECT * FROM governed_consolidation_proposals WHERE {where}", params).fetchone()
            if row is None:
                raise GovernedConsolidationError("proposal not found")
            return self._row_to_proposal(row)
        finally:
            connection.close()

    def list(self, *, project: str, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= MAX_PROPOSALS:
            raise GovernedConsolidationError(f"limit must be within 1..{MAX_PROPOSALS}")
        if status is not None and status not in STATUSES:
            raise GovernedConsolidationError("unsupported proposal status")
        connection = self._read()
        try:
            if status:
                rows = connection.execute(
                    "SELECT * FROM governed_consolidation_proposals WHERE project = ? AND status = ? ORDER BY created_at DESC, proposal_id LIMIT ?",
                    (project, status, int(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM governed_consolidation_proposals WHERE project = ? ORDER BY created_at DESC, proposal_id LIMIT ?",
                    (project, int(limit)),
                ).fetchall()
            return [self._row_to_proposal(row) for row in rows]
        finally:
            connection.close()

    def count_by_status(self) -> dict[str, int]:
        connection = self._read()
        try:
            counts = {str(row[0]): int(row[1]) for row in connection.execute("SELECT status, COUNT(*) FROM governed_consolidation_proposals GROUP BY status")}
            return {status: counts.get(status, 0) for status in sorted(STATUSES)}
        finally:
            connection.close()

    def decide(
        self,
        *,
        proposal_id: str,
        project: str,
        decision: str,
        actor: str,
        semantic_attestation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise GovernedConsolidationError("decision must be approve or reject")
        actor_digest = _sha256(_bounded_text(actor, limit=240, field="actor", required=True))
        now = _utc_now_iso()
        with self._write() as connection:
            row = connection.execute(
                "SELECT status, proposal_json FROM governed_consolidation_proposals WHERE proposal_id = ? AND project = ?",
                (proposal_id, project),
            ).fetchone()
            if row is None:
                raise GovernedConsolidationError("proposal not found")
            proposal = json.loads(str(row["proposal_json"]))
            validate_proposal_identity(proposal)
            status = str(row["status"])
            desired = "approved" if decision == "approve" else "rejected"
            if status == desired:
                if decision == "approve" and _is_semantic_proposal(proposal):
                    supplied = validate_semantic_operator_attestation(
                        semantic_attestation,
                        proposal=proposal,
                    )
                    recorded = self._validate_semantic_approval_event(
                        connection,
                        proposal_id=proposal_id,
                        proposal=proposal,
                    )
                    if supplied != recorded:
                        _semantic_attestation_error("approval event does not match supplied attestation")
                return self.get(proposal_id, project=project)
            if status != "proposed":
                raise GovernedConsolidationError(f"proposal cannot be {decision}d from {status}")
            validated_attestation = None
            if decision == "approve" and _is_semantic_proposal(proposal):
                validated_attestation = validate_semantic_operator_attestation(
                    semantic_attestation,
                    proposal=proposal,
                )
            column = "approved" if decision == "approve" else "rejected"
            connection.execute(
                f"UPDATE governed_consolidation_proposals SET status = ?, {column}_at = ?, {column}_by_digest = ?, updated_at = ? WHERE proposal_id = ?",
                (desired, now, actor_digest, now, proposal_id),
            )
            details: dict[str, Any] = {"actor_digest": actor_digest}
            if validated_attestation is not None:
                details["semantic_operator_attestation"] = validated_attestation
            self._append_event(connection, proposal_id, desired, now, details)
        return self.get(proposal_id, project=project)

    @staticmethod
    def _validate_semantic_approval_event(
        connection: sqlite3.Connection,
        *,
        proposal_id: str,
        proposal: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Require exactly one valid manual semantic approval event before apply."""

        rows = connection.execute(
            "SELECT details_json FROM governed_consolidation_events WHERE proposal_id = ? AND action = 'approved' ORDER BY created_at, event_id",
            (proposal_id,),
        ).fetchall()
        if len(rows) != 1:
            _semantic_attestation_error("exactly one approval event is required")
        try:
            details = json.loads(str(rows[0]["details_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            _semantic_attestation_error("approval event is malformed")
            raise AssertionError("unreachable") from exc
        if not isinstance(details, Mapping):
            _semantic_attestation_error("approval event is malformed")
        return validate_semantic_operator_attestation(
            details.get("semantic_operator_attestation"),
            proposal=proposal,
        )

    def validate_approved_semantic_attestation(self, proposal: Mapping[str, Any]) -> None:
        """Read-only event check used immediately before an approved semantic apply."""

        if not _is_semantic_proposal(proposal):
            return
        connection = self._read()
        try:
            self._validate_semantic_approval_event(
                connection,
                proposal_id=str(proposal["proposal_id"]),
                proposal=proposal,
            )
        finally:
            connection.close()

    def decide_automatically(
        self,
        *,
        proposal_id: str,
        project: str,
        decision: str,
        actor: str,
        policy_version: str,
        reason_codes: Iterable[str],
    ) -> dict[str, Any]:
        """Persist one deterministic policy decision without storing model text.

        This is deliberately separate from the operator decision surface.  The
        resulting proposal status is identical, while the event receipt makes
        the automated actor, policy version and bounded reason codes explicit.
        """

        if decision not in {"approve", "reject"}:
            raise GovernedConsolidationError("decision must be approve or reject")
        actor_digest = _sha256(_bounded_text(actor, limit=240, field="actor", required=True))
        normalized_policy = _bounded_text(policy_version, limit=120, field="policy_version", required=True)
        normalized_reasons = _bounded_items(list(reason_codes), limit=12, field="reason_codes")
        if not normalized_reasons:
            raise GovernedConsolidationError("reason_codes is required")
        now = _utc_now_iso()
        with self._write() as connection:
            row = connection.execute(
                "SELECT status, proposal_json FROM governed_consolidation_proposals WHERE proposal_id = ? AND project = ?",
                (proposal_id, project),
            ).fetchone()
            if row is None:
                raise GovernedConsolidationError("proposal not found")
            proposal = json.loads(str(row["proposal_json"]))
            validate_proposal_identity(proposal)
            if decision == "approve" and _is_semantic_proposal(proposal):
                raise GovernedConsolidationApprovalRequired(
                    "semantic proposals require a privileged operator attestation"
                )
            status = str(row["status"])
            desired = "approved" if decision == "approve" else "rejected"
            if status == desired:
                return self.get(proposal_id, project=project)
            if status != "proposed":
                raise GovernedConsolidationError(f"proposal cannot be automatically {decision}d from {status}")
            column = "approved" if decision == "approve" else "rejected"
            connection.execute(
                f"UPDATE governed_consolidation_proposals SET status = ?, {column}_at = ?, {column}_by_digest = ?, updated_at = ? WHERE proposal_id = ?",
                (desired, now, actor_digest, now, proposal_id),
            )
            self._append_event(
                connection,
                proposal_id,
                desired,
                now,
                {
                    "actor_digest": actor_digest,
                    "automatic": True,
                    "policy_version": normalized_policy,
                    "reason_codes": normalized_reasons,
                },
            )
        return self.get(proposal_id, project=project)

    @staticmethod
    def _append_event(connection: sqlite3.Connection, proposal_id: str, action: str, now: str, details: Mapping[str, Any]) -> None:
        event_digest = _sha256(_canonical_json({"proposal_id": proposal_id, "action": action, "at": now, "details": dict(details)}))
        connection.execute(
            "INSERT INTO governed_consolidation_events(event_id, proposal_id, action, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"gce_bhm_{event_digest[:24]}", proposal_id, action, _canonical_json(dict(details)), now),
        )

    def mark_stale(self, *, proposal_id: str, project: str, reason: str) -> None:
        now = _utc_now_iso()
        with self._write() as connection:
            row = connection.execute(
                "SELECT status FROM governed_consolidation_proposals WHERE proposal_id = ? AND project = ?",
                (proposal_id, project),
            ).fetchone()
            if row is None:
                raise GovernedConsolidationError("proposal not found")
            if str(row["status"]) in {"applied", "rejected"}:
                return
            connection.execute(
                "UPDATE governed_consolidation_proposals SET status = 'stale', stale_at = ?, failure_code = ?, updated_at = ? WHERE proposal_id = ?",
                (now, _bounded_text(reason, limit=160, field="reason", required=True), now, proposal_id),
            )
            self._append_event(connection, proposal_id, "stale", now, {"reason": reason})


def _proposal_mutations(proposal: Mapping[str, Any], repository: SQLiteMemoryRepository) -> tuple[list[Memory], MemoryLink | None, dict[str, str | None]]:
    operation = str(proposal["operation"])
    project = str(proposal["project"])
    candidate = dict(proposal["candidate"])
    basis = list(proposal["basis"])
    by_id = {str(item["memory_id"]): item for item in basis}
    expected = {memory_id: str(item["revision_id"]) for memory_id, item in by_id.items()}
    now = _utc_now_iso()
    if operation == "no_op":
        return [], None, expected
    target_id = str(candidate.get("target_memory_id") or basis[0]["memory_id"])
    target = repository.get_memory(target_id, project=project)
    if operation == "create":
        memory_id = f"mem_bhm_{_sha256(str(proposal['proposal_id']))[:24]}"
        revision_seed = f"{proposal['proposal_id']}:{candidate['content']}"
        revision_id = f"rev_bhm_{_sha256(revision_seed)[:24]}"
        memory = Memory(
            id=memory_id,
            project=project,
            memory_type=str(candidate["memory_type"]),
            memory_class=MemoryClass.SEMANTIC,
            memory_class_source=MemoryClassSource.DETERMINISTIC_RULE,
            memory_class_confidence=1.0,
            event_role=(
                MemoryEventRole.DECISION
                if str(candidate["memory_type"]).strip().casefold() in {"decision", "architecture", "policy"}
                else MemoryEventRole.FACT
            ),
            current_revision=MemoryRevision(revision_id=revision_id, memory_id=memory_id, content=str(candidate["content"]), content_sha256=content_sha256(str(candidate["content"])), created_at=now, created_by="governed-consolidation", metadata={"proposal_id": proposal["proposal_id"], "basis": basis}),
            provenance=Provenance(source_system="bhm", source_id=memory_id, source_kind="governed-consolidation", session_refs=()),
            title=str(candidate.get("title") or "") or None,
            summary=str(candidate.get("title") or "") or None,
            tags=tuple(candidate.get("concepts") or ()),
            files=tuple(candidate.get("files") or ()),
            session_refs=(),
            created_at=now,
            updated_at=now,
            metadata={"governed_consolidation": {"proposal_id": proposal["proposal_id"], "basis": basis, "operation": operation}},
        )
        retire_ids = tuple(dict.fromkeys(candidate.get("retire_basis_memory_ids") or ()))
        if not retire_ids:
            return [memory], None, expected
        basis_ids = set(by_id)
        if not set(retire_ids).issubset(basis_ids):
            raise GovernedConsolidationError("retire_basis_memory_ids must be an exact proposal basis subset")
        archived: list[Memory] = []
        for legacy_id in retire_ids:
            legacy = repository.get_memory(legacy_id, project=project)
            if legacy is None:
                raise GovernedConsolidationStale("legacy successor source is absent")
            metadata = dict(legacy.metadata)
            metadata.update(
                {
                    "archived_at": now,
                    "archive_reason": f"governed_consolidation_successor:{proposal['proposal_id']}",
                    "canonical_successor": {
                        "memory_id": memory.id,
                        "revision_id": memory.current_revision.revision_id,
                        "proposal_id": proposal["proposal_id"],
                    },
                    "governed_consolidation": {
                        "proposal_id": proposal["proposal_id"],
                        "basis": basis,
                        "operation": "create-and-retire-legacy",
                    },
                }
            )
            archived_payload = legacy.to_dict()
            archived_payload.update(
                {
                    "lifecycle": Lifecycle.ARCHIVED.value,
                    "updated_at": now,
                    "metadata": metadata,
                }
            )
            archived.append(Memory.from_dict(archived_payload))
        # One SQLite transaction saves the successor, archives each exact
        # source, and appends all projection events. No direct vector write.
        return [memory, *archived], None, expected
    if target is None:
        raise GovernedConsolidationStale("target memory is absent")
    if operation in {"revise", "supersede"}:
        updated_payload = target.to_dict()
        content = str(candidate["content"])
        revision_seed = f"{proposal['proposal_id']}:{target.id}:{content}"
        revision_id = f"rev_bhm_{_sha256(revision_seed)[:24]}"
        metadata = dict(target.metadata)
        metadata["governed_consolidation"] = {"proposal_id": proposal["proposal_id"], "basis": basis, "operation": operation}
        if operation == "supersede":
            metadata["supersedes_revision_id"] = target.current_revision.revision_id
        updated_payload.update({
            "title": str(candidate.get("title") or target.title or "") or None,
            "summary": str(candidate.get("title") or target.summary or "") or None,
            "tags": list(dict.fromkeys([*target.tags, *(candidate.get("concepts") or [])])),
            "files": list(dict.fromkeys([*target.files, *(candidate.get("files") or [])])),
            "updated_at": now,
            "metadata": metadata,
            "current_revision": {"revision_id": revision_id, "memory_id": target.id, "content": content, "content_sha256": content_sha256(content), "created_at": now, "created_by": "governed-consolidation", "metadata": {"proposal_id": proposal["proposal_id"], "basis": basis}},
        })
        return [Memory.from_dict(updated_payload)], None, expected
    if operation == "archive":
        archived_payload = target.to_dict()
        metadata = dict(target.metadata)
        metadata.update({
            "archived_at": now,
            "archive_reason": f"governed_consolidation:{proposal['proposal_id']}",
            "governed_consolidation": {"proposal_id": proposal["proposal_id"], "basis": basis, "operation": operation},
        })
        archived_payload.update({"lifecycle": Lifecycle.ARCHIVED.value, "updated_at": now, "metadata": metadata})
        return [Memory.from_dict(archived_payload)], None, expected
    if operation == "link":
        target_id = str(candidate["target_memory_id"])
        binding = proposal.get("identity_binding") if isinstance(proposal.get("identity_binding"), Mapping) else None
        bound_source = binding.get("source") if isinstance(binding, Mapping) and isinstance(binding.get("source"), Mapping) else None
        # Basis is canonically sorted for idempotency, so its first item may
        # be the explicit target.  Choose the first distinct same-project
        # basis endpoint rather than rejecting an otherwise valid two-memory
        # link proposal merely because of that stable ordering.
        source_id = str(bound_source.get("memory_id") or "") if bound_source is not None else next(
            (str(item["memory_id"]) for item in basis if str(item["memory_id"]) != target_id),
            "",
        )
        if not source_id:
            raise GovernedConsolidationError("link endpoints must differ")
        link_seed = f"{proposal['proposal_id']}:{source_id}:{target_id}:{candidate['relation']}"
        link_id = f"link_bhm_{_sha256(link_seed)[:24]}"
        return [], MemoryLink(id=link_id, project=project, source_id=source_id, target_id=target_id, relation=str(candidate["relation"]), created_at=now, updated_at=now, metadata={"proposal_id": proposal["proposal_id"], "basis": basis}), expected
    raise GovernedConsolidationError("unsupported operation")


def validate_proposal_current(*, proposal: Mapping[str, Any], repository: SQLiteMemoryRepository) -> dict[str, Any]:
    """Read-only optimistic-concurrency probe against canonical SQLite state."""

    validate_proposal_identity(proposal)
    project = str(proposal["project"])
    stale: list[str] = []
    for basis in proposal["basis"]:
        memory_id = str(basis["memory_id"])
        current = repository.get_memory(memory_id, project=project)
        if current is None:
            stale.append(f"missing:{memory_id}")
            continue
        if current.current_revision.revision_id != basis["revision_id"]:
            stale.append(f"revision:{memory_id}")
        elif current.current_revision.content_sha256 != basis["content_sha256"]:
            stale.append(f"digest:{memory_id}")
    return {"proposal_id": proposal["proposal_id"], "project": project, "current": not stale, "stale_reasons": stale, "execution": {"read_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}}


def dry_run_apply(*, proposal: Mapping[str, Any], repository: SQLiteMemoryRepository) -> dict[str, Any]:
    validation = validate_proposal_current(proposal=proposal, repository=repository)
    return {"proposal_id": proposal["proposal_id"], "status": proposal["status"], "can_apply": bool(proposal["status"] == "approved" and validation["current"]), "validation": validation, "requires": {"apply": True, "proposal_confirmation": proposal["proposal_id"], "human_approval": True}, "execution": {"dry_run": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}}


def _outbox_observability(repository: SQLiteMemoryRepository, event_ids: Iterable[str]) -> dict[str, Any]:
    """Report only the outbox work created by this apply; never drive it."""

    observed_at = datetime.now(timezone.utc)
    events = [event for event_id in event_ids if (event := repository.get_outbox_event(event_id)) is not None]
    status_counts: dict[str, int] = {}
    pending_ages: list[float] = []
    for event in events:
        status = event.status.value
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "completed":
            created_at = datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))
            pending_ages.append(max(0.0, (observed_at - created_at).total_seconds() * 1_000.0))
    return {
        "event_count": len(events),
        "status_counts": dict(sorted(status_counts.items())),
        "pending_projection_event_count": len(pending_ages),
        "projection_lag_ms": round(max(pending_ages, default=0.0), 3),
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
    }


def apply_approved_proposal(*, database_path: Path | str, proposal_id: str, project: str, apply: bool, confirmation: str) -> GovernedApplyResult:
    """Apply one exact approved proposal through the repository transaction."""

    started_at = time.perf_counter()
    if not apply or confirmation != proposal_id:
        raise GovernedConsolidationApprovalRequired("apply=true and exact proposal confirmation are required")
    proposal_store = GovernedConsolidationRepository(database_path)
    proposal = proposal_store.get(proposal_id, project=project)
    if proposal["status"] != "approved":
        raise GovernedConsolidationApprovalRequired("proposal is not approved")
    # A semantic approval is valid only when the event ledger still carries a
    # reviewer attestation bound to this exact proposal/evidence identity. This
    # catches direct proposal-json or event-json tampering before any lifecycle
    # transaction or outbox event is attempted.
    proposal_store.validate_approved_semantic_attestation(proposal)
    repository = SQLiteMemoryRepository(database_path)
    validation = validate_proposal_current(proposal=proposal, repository=repository)
    if not validation["current"]:
        proposal_store.mark_stale(proposal_id=proposal_id, project=project, reason=";".join(validation["stale_reasons"])[:160])
        raise GovernedConsolidationStale("proposal basis changed")
    try:
        memories, link, expected = _proposal_mutations(proposal, repository)
        results, stored_link = repository.apply_governed_consolidation(
            proposal_id=proposal_id,
            project=project,
            basis=proposal["basis"],
            memories=memories,
            expected_revision_ids=expected,
            link=link,
        )
    except (MemoryRevisionConflict, MemoryRepositoryIntegrityError, GovernedConsolidationStale) as exc:
        proposal_store.mark_stale(proposal_id=proposal_id, project=project, reason=type(exc).__name__)
        raise GovernedConsolidationStale("proposal basis changed before atomic apply") from exc
    outbox_event_ids = tuple(result.outbox_event_id for result in results)
    return GovernedApplyResult(
        proposal_id=proposal_id,
        status="applied",
        outbox_event_ids=outbox_event_ids,
        memory_ids=tuple(result.memory.id for result in results),
        link_id=stored_link.id if stored_link else None,
        apply_duration_ms=_elapsed_ms(started_at),
        outbox=_outbox_observability(repository, outbox_event_ids),
    )


__all__ = [
    "GOVERNED_CONSOLIDATION_ANALYZER", "GOVERNED_CONSOLIDATION_CAPABILITY_KEY", "GOVERNED_CONSOLIDATION_CAPABILITY_VERSION", "GOVERNED_CONSOLIDATION_SCHEMA_VERSION", "GovernedApplyResult", "GovernedConsolidationApprovalRequired", "GovernedConsolidationDisabled", "GovernedConsolidationError", "GovernedConsolidationMigrationRequired", "GovernedConsolidationRepository", "GovernedConsolidationStale", "analyze_records", "apply_approved_proposal", "build_proposal", "dry_run_apply", "runtime_enabled", "runtime_status", "validate_proposal_current",
]
