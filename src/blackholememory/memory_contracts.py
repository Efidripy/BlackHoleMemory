"""Shared REST/MCP memory metadata contract.

This module is intentionally transport-neutral.  REST and MCP adapters import
the same Pydantic model so schema drift cannot silently reappear between the
two surfaces.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class MetadataLifecycle(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class MetadataProvenance(str, Enum):
    GITHUB = "github"
    MCP = "mcp"
    LLM = "llm"
    HUMAN = "human"
    SYNTHETIC = "synthetic"


class MetadataPriority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NORMAL = "normal"
    TRIVIAL = "trivial"


class MetadataDomain(str, Enum):
    FRONTEND = "frontend"
    BACKEND = "backend"
    INFRA = "infra"
    SECURITY = "security"
    PRODUCT = "product"
    GENERAL = "general"


class MetadataSensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class MetadataScope(str, Enum):
    GLOBAL = "global"
    SERVICE = "service"
    FEATURE = "feature"
    LOCAL = "local"


class MetadataRetention(str, Enum):
    TRANSIENT = "transient"
    SHORT_TERM = "short-term"
    LONG_TERM = "long-term"
    PERMANENT = "permanent"


class MetadataVerification(str, Enum):
    UNVERIFIED = "unverified"
    PEER_REVIEWED = "peer-reviewed"
    TRUSTED = "trusted"


class MetadataActionability(str, Enum):
    TASK = "task"
    INFO = "info"
    DECISION = "decision"
    QUERY = "query"


class MetadataStakeholder(str, Enum):
    CORE_TEAM = "core-team"
    DEVOPS = "devops"
    FRONTEND_SQUAD = "frontend-squad"
    PRODUCT_OWNER = "product-owner"


class MetadataLanguage(str, Enum):
    EN = "en"
    RU = "ru"
    CODE_PYTHON = "code-python"
    CODE_TS = "code-ts"


class MetadataSemanticType(str, Enum):
    ARCHITECTURE = "architecture"
    BUGFIX = "bugfix"
    FEATURE = "feature"
    REFACTOR = "refactor"
    KNOWLEDGE = "knowledge"
    FACT = "fact"
    LOG = "log"
    ERROR = "error"
    DECISION_LOG = "decision-log"
    REQUIREMENT = "requirement"


class MemoryClass(str, Enum):
    """Stable cognitive class independent from the legacy record taxonomy.

    ``memory_type`` is already a public free-form field used by historical
    records such as ``workflow``, ``checkpoint`` and ``architecture``.  WL-300
    therefore exposes the new closed vocabulary as the additive
    ``memory_class`` field instead of silently reinterpreting existing data.
    """

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    WORKING = "working"
    UNCLASSIFIED = "unclassified"


class MemoryClassSource(str, Enum):
    """Auditable origin of a cognitive classification."""

    LEGACY_DEFAULT = "legacy-default"
    REQUEST_DEFAULT = "request-default"
    CALLER_EXPLICIT = "caller-explicit"
    DETERMINISTIC_RULE = "deterministic-rule"
    REVIEW_CONFIRMED = "review-confirmed"


class MemoryEventRole(str, Enum):
    """Operational role independent from cognitive and legacy taxonomies."""

    FACT = "fact"
    DECISION = "decision"
    QA = "qa"
    TRACE = "trace"
    FEEDBACK = "feedback"
    SKILL_RUN = "skill_run"
    UNCLASSIFIED = "unclassified"


EVENT_ROLE_SCHEMA_VERSION = "1"
SUPPORTED_EVENT_ROLE_VERSIONS = frozenset({EVENT_ROLE_SCHEMA_VERSION})


class ProcedureValueType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"
    NULL = "null"


class ProcedureRollbackMode(str, Enum):
    REQUIRED = "required"
    NOT_APPLICABLE = "not-applicable"


class ProcedureTraceStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled-back"


class ProcedureStepStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProcedureRollbackStatus(str, Enum):
    NOT_REQUIRED = "not-required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProcedureValueSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    value_type: ProcedureValueType
    required: bool = True
    description: str | None = Field(default=None, max_length=1000)


class ProcedureCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_id: str = Field(min_length=1, max_length=128)
    assertion: str = Field(min_length=1, max_length=2000)
    required: bool = True


class ProcedureStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=8000)
    depends_on: list[str] = Field(default_factory=list, max_length=64)


class ProcedureApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requires_operator_approval: Literal[True] = True
    approval_digest_required: Literal[True] = True
    approver_role: str = Field(default="operator", min_length=1, max_length=128)


class ProcedureRollbackPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ProcedureRollbackMode = ProcedureRollbackMode.REQUIRED
    rollback_anchor_required: bool = True
    instructions: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def _validate_rollback(self) -> "ProcedureRollbackPolicy":
        if self.mode is ProcedureRollbackMode.REQUIRED and not self.instructions:
            raise ValueError("required rollback policy must declare instructions")
        if self.mode is ProcedureRollbackMode.NOT_APPLICABLE and self.rollback_anchor_required:
            raise ValueError("not-applicable rollback cannot require a rollback anchor")
        return self


class MemoryClassProposal(BaseModel):
    """LLM/rule proposal only; it never changes the authoritative class."""

    model_config = ConfigDict(extra="forbid")

    proposed_class: MemoryClass
    confidence: float = Field(ge=0.0, le=1.0)
    proposer: str = Field(min_length=1, max_length=256)
    rule_or_model_version: str = Field(min_length=1, max_length=128)
    proposal_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _bind_digest(self) -> "MemoryClassProposal":
        payload = self.model_dump(mode="json", exclude={"proposal_digest"})
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.proposal_digest is not None and self.proposal_digest != expected:
            raise ValueError("memory class proposal digest mismatch")
        object.__setattr__(self, "proposal_digest", expected)
        return self


class ProcedureContract(BaseModel):
    """Declarative procedure data. It cannot authorize or execute itself."""

    model_config = ConfigDict(extra="forbid")

    procedure_version: str = Field(min_length=1, max_length=64)
    inputs: list[ProcedureValueSpec] = Field(default_factory=list, max_length=128)
    outputs: list[ProcedureValueSpec] = Field(default_factory=list, max_length=128)
    preconditions: list[ProcedureCondition] = Field(default_factory=list, max_length=128)
    steps: list[ProcedureStep] = Field(min_length=1, max_length=256)
    postconditions: list[ProcedureCondition] = Field(default_factory=list, max_length=128)
    approval_policy: ProcedureApprovalPolicy = Field(default_factory=ProcedureApprovalPolicy)
    rollback_policy: ProcedureRollbackPolicy
    auto_execute: Literal[False] = False
    self_modification: Literal[False] = False
    memory_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_graph_and_digest(self) -> "ProcedureContract":
        for label, specs in (("input", self.inputs), ("output", self.outputs)):
            names = [spec.name for spec in specs]
            if len(names) != len(set(names)):
                raise ValueError(f"procedure {label} names must be unique")
        for label, conditions in (("precondition", self.preconditions), ("postcondition", self.postconditions)):
            ids = [condition.condition_id for condition in conditions]
            if len(ids) != len(set(ids)):
                raise ValueError(f"procedure {label} ids must be unique")
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("procedure step_id values must be unique")
        known_steps = set(step_ids)
        for position, step in enumerate(self.steps):
            unknown = sorted(set(step.depends_on) - known_steps)
            if unknown:
                raise ValueError(
                    f"procedure step {step.step_id} depends on unknown step(s): {', '.join(unknown)}"
                )
            earlier = set(step_ids[:position])
            if not set(step.depends_on).issubset(earlier):
                raise ValueError("procedure steps may depend only on earlier ordered steps")
        payload = self.model_dump(mode="json", exclude={"content_digest"})
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.content_digest is not None and self.content_digest != expected:
            raise ValueError("procedure content_digest does not match the canonical contract")
        object.__setattr__(self, "content_digest", expected)
        return self


class ProcedureStepTraceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=128)
    status: ProcedureStepStatus
    started_at: str = Field(min_length=1, max_length=64)
    completed_at: str = Field(min_length=1, max_length=64)
    input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = Field(default=None, max_length=128)


class ProcedureExecutionTraceReceipt(BaseModel):
    """Content-free storage receipt. This model deliberately has no executor."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["bhm.procedure-trace.v1"] = "bhm.procedure-trace.v1"
    execution_id: str = Field(min_length=1, max_length=256)
    project: str = Field(min_length=1, max_length=256)
    memory_id: str = Field(min_length=1, max_length=256)
    procedure_version: str = Field(min_length=1, max_length=64)
    procedure_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ProcedureTraceStatus
    started_at: str = Field(min_length=1, max_length=64)
    completed_at: str = Field(min_length=1, max_length=64)
    steps: list[ProcedureStepTraceReceipt] = Field(min_length=1, max_length=256)
    rollback_status: ProcedureRollbackStatus = ProcedureRollbackStatus.NOT_REQUIRED
    rollback_receipt_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    receipt_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_and_bind_digest(self) -> "ProcedureExecutionTraceReceipt":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("procedure trace step ids must be unique")
        if self.rollback_status is ProcedureRollbackStatus.NOT_REQUIRED and self.rollback_receipt_digest is not None:
            raise ValueError("not-required rollback cannot carry a rollback receipt")
        if self.rollback_status is not ProcedureRollbackStatus.NOT_REQUIRED and self.rollback_receipt_digest is None:
            raise ValueError("rollback status requires rollback_receipt_digest")
        payload = self.model_dump(mode="json", exclude={"receipt_digest"})
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.receipt_digest is not None and self.receipt_digest != expected:
            raise ValueError("procedure trace receipt digest mismatch")
        object.__setattr__(self, "receipt_digest", expected)
        return self


class MemoryMetadata(BaseModel):
    model_config = ConfigDict(extra="allow", use_enum_values=True)

    lifecycle: MetadataLifecycle | None = Field(default=None, description="draft/validated/deprecated/archived")
    provenance: MetadataProvenance | None = Field(default=None, description="github/mcp/llm/human/synthetic")
    priority: MetadataPriority | None = Field(default=None, description="critical/high/medium/low; normal/trivial are legacy aliases")
    domain: MetadataDomain | None = Field(default=None, description="frontend/backend/infra/security/product/general")
    sensitivity: MetadataSensitivity | None = Field(default=None, description="public/internal/restricted")
    scope: MetadataScope | None = Field(default=None, description="global/service/feature/local")
    retention: MetadataRetention | None = Field(default=None, description="transient/short-term/long-term/permanent")
    verification: MetadataVerification | None = Field(default=None, description="unverified/peer-reviewed/trusted")
    actionability: MetadataActionability | None = Field(default=None, description="task/info/decision/query")
    stakeholder: MetadataStakeholder | None = Field(default=None, description="core-team/devops/frontend-squad/product-owner")
    language: MetadataLanguage | None = Field(default=None, description="en/ru/code-python/code-ts")
    semantic_type: MetadataSemanticType | None = Field(
        default=None,
        description="architecture/bugfix/feature/refactor/knowledge; fact/log/error/decision-log/requirement are legacy values",
    )
    memory_class: MemoryClass | None = Field(
        default=None,
        description="episodic/semantic/procedural/working/unclassified cognitive memory class",
    )
    memory_class_source: MemoryClassSource | None = Field(
        default=None,
        description="Auditable source of the cognitive memory classification.",
    )
    memory_class_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional confidence for deterministic/reviewed classification.",
    )
    memory_class_rule_id: str | None = Field(default=None, max_length=128)
    memory_class_rule_version: str | None = Field(default=None, max_length=64)
    memory_class_confirmed_by: str | None = Field(default=None, max_length=256)
    memory_class_proposal: MemoryClassProposal | None = None
    event_role: MemoryEventRole | None = Field(
        default=None,
        description="fact/decision/qa/trace/feedback/skill_run/unclassified operational role",
    )
    event_role_version: str | None = Field(
        default=None,
        description="Version of the event-role allowlist; current value is 1.",
    )
    procedure_contract: ProcedureContract | None = Field(
        default=None,
        description="Declarative, digest-bound procedural contract; never execution authority.",
    )
    procedure_trace_receipt: ProcedureExecutionTraceReceipt | None = Field(
        default=None,
        description="Content-free execution evidence stored as an explicit trace event.",
    )
    version: str | None = Field(default=None, description='Taxonomy version, for example "1.0".')
    importance_score: int | None = Field(default=None, ge=1, le=10, description="Cognitive importance from 1 to 10.")

    @model_validator(mode="after")
    def _validate_event_role_version(self) -> "MemoryMetadata":
        if self.event_role_version is not None and self.event_role_version not in SUPPORTED_EVENT_ROLE_VERSIONS:
            raise ValueError(f"event_role_version must be {EVENT_ROLE_SCHEMA_VERSION}")
        return self


__all__ = [
    "EVENT_ROLE_SCHEMA_VERSION",
    "SUPPORTED_EVENT_ROLE_VERSIONS",
    "MemoryClass",
    "MemoryClassProposal",
    "MemoryClassSource",
    "MemoryEventRole",
    "MemoryMetadata",
    "MetadataActionability",
    "MetadataDomain",
    "MetadataLanguage",
    "MetadataLifecycle",
    "MetadataPriority",
    "MetadataProvenance",
    "MetadataRetention",
    "MetadataScope",
    "MetadataSemanticType",
    "MetadataSensitivity",
    "MetadataStakeholder",
    "MetadataVerification",
    "ProcedureApprovalPolicy",
    "ProcedureCondition",
    "ProcedureContract",
    "ProcedureExecutionTraceReceipt",
    "ProcedureRollbackMode",
    "ProcedureRollbackPolicy",
    "ProcedureRollbackStatus",
    "ProcedureStep",
    "ProcedureStepStatus",
    "ProcedureStepTraceReceipt",
    "ProcedureTraceStatus",
    "ProcedureValueType",
    "ProcedureValueSpec",
]
