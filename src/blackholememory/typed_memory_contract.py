"""WL-300.1 typed-memory governance and rollback feature boundary."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .filesystem_boundaries import assert_safe_path
from .memory_contracts import EVENT_ROLE_SCHEMA_VERSION
from .memory_contracts import MemoryClass
from .memory_contracts import MemoryClassSource
from .memory_contracts import MemoryEventRole
from .memory_contracts import ProcedureContract


FEATURE_FLAG = "BHM_TYPED_MEMORY_CONTRACT_ENABLED"
CAPABILITY_KEY = "typed_memory_contract_version"
CAPABILITY_VERSION = "1"
CLASSIFIER_RULE_VERSION = "bhm.memory-class.rules.v1"

TYPED_MEMORY_COLUMNS = frozenset(
    {
        "memory_class",
        "memory_class_source",
        "memory_class_confidence",
        "event_role",
        "event_role_version",
    }
)
TYPED_MEMORY_INDEXES = frozenset(
    {
        "idx_memories_project_class_lifecycle_time",
        "idx_memories_project_event_role_lifecycle_time",
    }
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

_SEMANTIC_TYPES = frozenset(
    {"architecture", "decision", "fact", "knowledge", "pattern", "requirement"}
)
_EPISODIC_TYPES = frozenset(
    {"checkpoint", "error", "handoff", "log", "observation", "qa", "session", "trace"}
)
_WORKING_TYPES = frozenset({"draft", "scratchpad", "working"})


class TypedMemoryContractUnavailable(RuntimeError):
    """A typed operation was requested outside the enabled/capable boundary."""


@dataclass(frozen=True)
class MemoryClassification:
    memory_class: MemoryClass
    source: MemoryClassSource
    confidence: float | None
    rule_id: str | None
    rule_version: str | None


def typed_memory_contract_enabled() -> bool:
    # The additive schema is intentionally dark until an operator has applied
    # and verified the migration.  Legacy reads/writes remain available while
    # typed intent is rejected fail-closed by the API boundary.
    raw = str(os.getenv(FEATURE_FLAG, "false") or "").strip().casefold()
    if raw in _FALSE_VALUES:
        return False
    if raw in _TRUE_VALUES or not raw:
        return True
    return False


def typed_memory_capability_available(database: str | Path) -> bool:
    """Return whether the additive SQLite capability is fully present."""

    path = assert_safe_path(database).resolve()
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0) as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(memories)").fetchall()
            }
            indexes = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            marker = connection.execute(
                "SELECT value FROM memory_store_meta WHERE key = ?",
                (CAPABILITY_KEY,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return False
    return (
        TYPED_MEMORY_COLUMNS.issubset(columns)
        and TYPED_MEMORY_INDEXES.issubset(indexes)
        and marker is not None
        and str(marker[0]) == CAPABILITY_VERSION
    )


def require_typed_memory_contract(*, capability_available: bool) -> None:
    if not typed_memory_contract_enabled():
        raise TypedMemoryContractUnavailable("typed_memory_contract_disabled")
    if not capability_available:
        raise TypedMemoryContractUnavailable("typed_memory_contract_migration_required")


def classify_new_memory(
    *,
    explicit_class: MemoryClass | None,
    memory_type: str,
    event_role: MemoryEventRole | None,
    procedure_contract: ProcedureContract | None,
    confirmed_by: str | None = None,
    capability_available: bool,
) -> MemoryClassification:
    """Resolve only new writes; existing legacy rows are never reclassified."""

    typed_requested = any(
        value is not None
        for value in (explicit_class, event_role, procedure_contract, confirmed_by)
    )
    if not typed_memory_contract_enabled() or not capability_available:
        if typed_requested:
            require_typed_memory_contract(capability_available=capability_available)
        return MemoryClassification(
            MemoryClass.UNCLASSIFIED,
            MemoryClassSource.REQUEST_DEFAULT,
            None,
            None,
            None,
        )

    if explicit_class is not None:
        if confirmed_by:
            return MemoryClassification(
                explicit_class,
                MemoryClassSource.REVIEW_CONFIRMED,
                1.0,
                "operator-confirmation",
                CLASSIFIER_RULE_VERSION,
            )
        return MemoryClassification(
            explicit_class,
            MemoryClassSource.CALLER_EXPLICIT,
            1.0,
            None,
            None,
        )

    normalized_type = str(memory_type or "").strip().casefold()
    normalized_role = event_role or MemoryEventRole.UNCLASSIFIED
    if procedure_contract is not None:
        return MemoryClassification(
            MemoryClass.PROCEDURAL,
            MemoryClassSource.DETERMINISTIC_RULE,
            1.0,
            "procedure-contract-present",
            CLASSIFIER_RULE_VERSION,
        )
    if normalized_type in _SEMANTIC_TYPES or normalized_role in {
        MemoryEventRole.FACT,
        MemoryEventRole.DECISION,
    }:
        return MemoryClassification(
            MemoryClass.SEMANTIC,
            MemoryClassSource.DETERMINISTIC_RULE,
            1.0,
            "semantic-type-or-role",
            CLASSIFIER_RULE_VERSION,
        )
    if normalized_type in _EPISODIC_TYPES or normalized_role in {
        MemoryEventRole.QA,
        MemoryEventRole.TRACE,
        MemoryEventRole.FEEDBACK,
        MemoryEventRole.SKILL_RUN,
    }:
        return MemoryClassification(
            MemoryClass.EPISODIC,
            MemoryClassSource.DETERMINISTIC_RULE,
            1.0,
            "episodic-type-or-role",
            CLASSIFIER_RULE_VERSION,
        )
    if normalized_type in _WORKING_TYPES:
        return MemoryClassification(
            MemoryClass.WORKING,
            MemoryClassSource.DETERMINISTIC_RULE,
            1.0,
            "working-type",
            CLASSIFIER_RULE_VERSION,
        )
    return MemoryClassification(
        MemoryClass.UNCLASSIFIED,
        MemoryClassSource.REQUEST_DEFAULT,
        None,
        None,
        None,
    )


def projection_contract_fields(memory: Any) -> dict[str, Any]:
    """Return only bounded typed fields safe for rebuildable projection."""

    if not typed_memory_contract_enabled():
        return {}
    metadata = dict(getattr(memory, "metadata", {}) or {})
    procedure = getattr(memory, "procedure_contract", None)
    trace = getattr(memory, "procedure_trace_receipt", None)
    fields: dict[str, Any] = {
        "memory_class": getattr(getattr(memory, "memory_class", None), "value", "unclassified"),
        "memory_class_source": getattr(
            getattr(memory, "memory_class_source", None), "value", "legacy-default"
        ),
        "memory_class_confidence": getattr(memory, "memory_class_confidence", None),
        "event_role": getattr(getattr(memory, "event_role", None), "value", "unclassified"),
        "event_role_version": getattr(memory, "event_role_version", EVENT_ROLE_SCHEMA_VERSION),
    }
    if procedure is not None:
        fields["procedure_version"] = procedure.procedure_version
        fields["procedure_digest"] = procedure.content_digest
    elif isinstance(metadata.get("procedure_contract"), dict):
        fields["procedure_version"] = metadata["procedure_contract"].get("procedure_version")
        fields["procedure_digest"] = metadata["procedure_contract"].get("content_digest")
    if trace is not None:
        fields["procedure_trace_receipt_digest"] = trace.receipt_digest
    return fields


__all__ = [
    "CAPABILITY_KEY",
    "CAPABILITY_VERSION",
    "CLASSIFIER_RULE_VERSION",
    "FEATURE_FLAG",
    "MemoryClassification",
    "TYPED_MEMORY_COLUMNS",
    "TYPED_MEMORY_INDEXES",
    "TypedMemoryContractUnavailable",
    "classify_new_memory",
    "projection_contract_fields",
    "require_typed_memory_contract",
    "typed_memory_capability_available",
    "typed_memory_contract_enabled",
]
