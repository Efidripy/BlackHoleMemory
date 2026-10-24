from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .api_models_services import (
    CapabilityRoutePreviewRequest,  # noqa: F401
    ChangeImpactPreviewRequest,  # noqa: F401
    CodeGraphQueryRequest,  # noqa: F401
    ConventionMemoryPreviewRequest,  # noqa: F401
    DocumentationFactoryPreviewRequest,  # noqa: F401
    FactoryIntegrationPreviewRequest,  # noqa: F401
    HumanUiBridgePreviewRequest,  # noqa: F401
    LLMCachePreviewRequest,  # noqa: F401
    LLMCandidatePlanRequest,  # noqa: F401
    LLMCodeFabricPlanRequest,  # noqa: F401
    LLMDelegationDecisionRequest,  # noqa: F401
    LLMJobSubmitRequest,  # noqa: F401
    LLMLearningCurateRequest,  # noqa: F401
    LLMLearningReviewRequest,  # noqa: F401
    MemoryFoundryPreviewRequest,  # noqa: F401
    MemoryGraphQueryRequest,  # noqa: F401
    MigrationPreviewRequest,  # noqa: F401
    ModelRouterDecisionRequest,  # noqa: F401
    NightShiftPreviewRequest,  # noqa: F401
    PublicCodeToolRequest,  # noqa: F401
    QAIncidentPreviewRequest,  # noqa: F401
    RepositoryIntelligencePreviewRequest,  # noqa: F401
    RetrievalLabPreviewRequest,  # noqa: F401
    SecurityTrustBoundaryPreviewRequest,  # noqa: F401
    SessionCapturePreviewRequest,  # noqa: F401
    TaskGraphQueryRequest,  # noqa: F401
    UnifiedContextCompileRequest,  # noqa: F401
    UnifiedMcpContractPreviewRequest,  # noqa: F401
)
from .config import settings
from .context_compiler import MAX_CONTEXT_TOKEN_BUDGET
from .governed_shared_memory import SharedOperation, SharedVisibility
from .memory_contracts import MemoryClass, MemoryEventRole, MemoryMetadata
from .utility_feedback import UtilityEventType

FactCrystalDomain = Literal["frontend", "backend", "infra", "security", "product", "general"]
FactCrystalPriority = Literal["low", "medium", "high", "critical"]
FactCrystalSemanticType = Literal["architecture", "bugfix", "feature", "refactor", "knowledge"]
_CUSTOM_REDACTION_MAX_PATTERNS = 16

class UiSessionExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_token: StrictStr = Field(min_length=32, max_length=256)

class SearchRequest(BaseModel):
    query: str
    project: str | None = None
    user_id: str = settings.mem0_user_id
    # Keep federated/compatibility retrieval bounded; the fallback path already
    # caps at 200, so accepting larger values only creates timeout-prone work.
    top_k: int = Field(default=5, ge=1, le=200)
    domain: str | None = None
    semantic_type: str | None = None
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    as_of: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    include_temporal_unknown: bool = False
    priority: str | None = None
    include_archived: bool = False
    include_logs: bool = False
    include_historical: bool = False
    # Read-only freshness window based on the authoritative updated_at field.
    freshness_days: int | None = Field(default=None, ge=1, le=3650)
    history_scope: Literal["current", "recent", "all"] | None = None

class RememberRequest(BaseModel):
    project: str = "e-github-workspace"
    type: str = "workflow"
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    observed_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    open_interval: bool | None = None
    supersedes_revision_id: str | None = None
    source_episode_id: str | None = None
    source_uri: str | None = None
    source_digest: str | None = None
    content: str
    concepts: list[str] | None = None
    files: list[str] | None = None
    upsert_key: str | None = None
    metadata: MemoryMetadata | None = None

class MemoryUpdateRequest(BaseModel):
    id: str
    project: str | None = None
    type: str | None = None
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    observed_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    open_interval: bool | None = None
    supersedes_revision_id: str | None = None
    source_episode_id: str | None = None
    source_uri: str | None = None
    source_digest: str | None = None
    content: str | None = None
    concepts: list[str] | None = None
    files: list[str] | None = None
    metadata_patch: MemoryMetadata | None = None

class MemoryArchiveRequest(BaseModel):
    id: str
    project: str | None = None
    reason: str = ""

class ForgetPreviewRequest(BaseModel):
    project: str | None = None
    memory_ids: list[str] = Field(default_factory=list, max_length=50)
    upsert_keys: list[str] = Field(default_factory=list, max_length=50)
    operation: Literal["tombstone", "undo"] = "tombstone"
    reason: str = Field(default="forget", max_length=200)
    undo_window_seconds: int = Field(default=900, ge=1, le=604800)
    limit: int = Field(default=50, ge=1, le=200)

class ForgetApplyRequest(ForgetPreviewRequest):
    preview_digest: str = Field(min_length=64, max_length=64)
    confirm: bool = False

class MemoryAdvancedSearchRequest(BaseModel):
    query: str = ""
    project: str | None = None
    memory_type: str | None = None
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    as_of: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    include_temporal_unknown: bool = False
    concepts: list[str] | None = None
    files: list[str] | None = None
    include_archived: bool = False
    include_logs: bool = False
    include_historical: bool = False
    # Read-only freshness window based on the authoritative updated_at field.
    freshness_days: int | None = Field(default=None, ge=1, le=3650)
    history_scope: Literal["current", "recent", "all"] | None = None
    domain: str | None = None
    semantic_type: str | None = None
    priority: str | None = None
    limit: int = 10
    offset: int = 0

class ContextCompileRequest(BaseModel):
    """Bounded, project-scoped context assembly request."""

    query: str
    project: str | None = None
    memory_type: str | None = None
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    as_of: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    include_temporal_unknown: bool = False
    concepts: list[str] | None = None
    files: list[str] | None = None
    include_archived: bool = False
    include_logs: bool = False
    include_historical: bool = False
    # Keep context assembly on the same explicit history contract as search
    # and Galaxy. ``include_historical`` remains the compatibility alias.
    freshness_days: int | None = Field(default=None, ge=1, le=3650)
    history_scope: Literal["current", "recent", "all"] | None = None
    domain: str | None = None
    semantic_type: str | None = None
    priority: str | None = None
    profile: str | None = None
    tiered_context: bool = False
    limit: int | None = Field(default=None, ge=1, le=50)
    token_budget: int | None = Field(default=None, ge=64, le=MAX_CONTEXT_TOKEN_BUDGET)

class AgentContextRequest(BaseModel):
    """One bounded, SQLite-authoritative read package for an active agent."""

    project: str
    query: str | None = Field(default=None, max_length=500)
    retrieval_limit: int = Field(default=8, ge=1, le=12)
    token_budget: int = Field(default=800, ge=64, le=2_000)
    active_task_limit: int = Field(default=10, ge=1, le=10)

class ContextTierPromotionRollbackRequest(BaseModel):
    """Admin-only reversal of one exact project-scoped promotion receipt."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=160)
    candidate_id: str = Field(min_length=8, max_length=160)
    apply: StrictBool = False
    confirmation: str = Field(min_length=8, max_length=160)

class RetrievalExplainRequest(BaseModel):
    """Request for bounded ranking and routing diagnostics."""

    query: str
    project: str | None = None
    memory_type: str | None = None
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    as_of: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    include_temporal_unknown: bool = False
    concepts: list[str] | None = None
    files: list[str] | None = None
    include_archived: bool = False
    include_logs: bool = False
    include_historical: bool = False
    domain: str | None = None
    semantic_type: str | None = None
    priority: str | None = None
    limit: int = Field(default=10, ge=1, le=50)

class MemoryUsedRequest(BaseModel):
    """Explicit access-feedback signal for already retrieved memory ids."""

    ids: list[str] = Field(min_length=1, max_length=50)
    project: str | None = None
    reason: str = Field(default="", max_length=200)

class UtilityFeedbackEventRequest(BaseModel):
    """One authenticated, immutable utility signal for an existing memory."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=160)
    event_id: str = Field(min_length=1, max_length=160)
    memory_id: str = Field(min_length=1, max_length=160)
    event_type: UtilityEventType
    observed_at: str = Field(min_length=20, max_length=64)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

class ConsolidationChangeSetPreviewRequest(BaseModel):
    """Bounded, redacted detector input for an operator-only change-set preview."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=160)
    as_of: str = Field(min_length=20, max_length=64)
    candidates: list[dict[str, Any]] = Field(min_length=1, max_length=128)
    max_actions: int = Field(default=64, ge=1, le=128)

class ConsolidationChangeSetReviewRequest(BaseModel):
    """Authenticated operator decision over one freshly regenerated preview."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=160)
    as_of: str = Field(min_length=20, max_length=64)
    candidates: list[dict[str, Any]] = Field(min_length=1, max_length=128)
    change_set: dict[str, Any]
    review_id: str = Field(min_length=1, max_length=96)
    decision: Literal["approved_no_apply", "rejected", "deferred"]
    action_ids: list[str] = Field(min_length=1, max_length=128)
    reviewed_at: str = Field(min_length=20, max_length=64)
    rationale_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_actions: int = Field(default=64, ge=1, le=128)

class GovernedConsolidationCreateRequest(BaseModel):
    """Create one same-project, non-persisting consolidation proposal."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=160)
    memory_ids: list[str] = Field(min_length=1, max_length=32)
    operation: Literal["no_op", "create", "revise", "supersede", "archive", "link"] = "create"
    candidate: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=480)
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)

class GovernedSemanticProposalRequest(BaseModel):
    """Run one bounded local semantic pass; history is explicit opt-in."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=1, max_length=480)
    limit: int = Field(default=12, ge=1, le=20)
    store_proposal: StrictBool = False
    include_historical: StrictBool = False

class GovernedConsolidationDecisionRequest(BaseModel):
    """Record a human decision; semantic approvals carry a bound attestation."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=160)
    proposal_id: str = Field(min_length=8, max_length=96)
    decision: Literal["approve", "reject"]
    semantic_attestation: dict[str, Any] | None = None

class GovernedConsolidationProposalRequest(BaseModel):
    """Reference one project-scoped proposal without requesting a mutation."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=160)
    proposal_id: str = Field(min_length=8, max_length=96)

class GovernedConsolidationApplyRequest(BaseModel):
    """Apply one approved proposal only after explicit ID-bound confirmation."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=160)
    proposal_id: str = Field(min_length=8, max_length=96)
    apply: StrictBool = False
    confirmation: str = Field(min_length=8, max_length=96)

class McpRepairRequest(BaseModel):
    """Bounded BHM-only repair action; client restart remains outside BHM ownership."""

    model_config = ConfigDict(extra="forbid")

    clients: list[StrictStr] = Field(default_factory=list, max_length=2)
    repair_id: StrictStr | None = Field(default=None, min_length=27, max_length=27)
    confirm: StrictBool = False
    apply_adapters: StrictBool = False

class MemoryRecentActivityRequest(BaseModel):
    project: str | None = None
    memory_type: str | None = None
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    as_of: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    include_temporal_unknown: bool = False
    include_archived: bool = False
    limit: int = 10

class GalaxyDataNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    type: str = "memory"
    val: float = 4.2
    color: str = "#87f5c9"
    core_insight: str = ""
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

class GalaxyDataLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    type: str

class GalaxyDataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[GalaxyDataNode] = Field(default_factory=list)
    links: list[GalaxyDataLink] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)

class GalaxyStatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "bhm.galaxy.stats.v1"
    scope: str = "all-projects"
    node_count: int = 0
    link_count: int = 0
    authority: str = "sqlite-authoritative"
    bounded: bool = False
    limit: int | None = None

class LauncherStatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "bhm.launcher.stats.v1"
    scope: str = "all-projects"
    memory_count: int = 0
    session_count: int = 0
    memory_authority: str = "sqlite-authoritative"
    session_authority: str = "runtime-artifact-store"
    bounded: bool = True

class MemoryUpsertRequest(BaseModel):
    upsert_key: str
    project: str = "e-github-workspace"
    type: str = "workflow"
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    observed_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    open_interval: bool | None = None
    supersedes_revision_id: str | None = None
    source_episode_id: str | None = None
    source_uri: str | None = None
    source_digest: str | None = None
    content: str
    concepts: list[str] | None = None
    files: list[str] | None = None
    metadata: MemoryMetadata | None = None

class BatchMemoryUpsertItem(MemoryUpsertRequest):
    """Batch item with an optional project inherited from the batch scope."""

    project: str | None = None

class MemoryLinkRequest(BaseModel):
    source_id: str
    target_id: str
    relation: str
    project: str
    metadata: MemoryMetadata | None = None
    ontology_schema_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

class BatchMemoryLinkItem(MemoryLinkRequest):
    project: str

class MemoryLinkDeleteRequest(BaseModel):
    source_id: str
    target_id: str
    relation: str
    project: str

class SharedMemoryPolicyPreflightRequest(BaseModel):
    """Evaluate governance only; this request cannot read or write memory."""

    project: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    operation: SharedOperation
    visibility: SharedVisibility
    owner_id: str = Field(min_length=1, max_length=256)
    at: str = Field(min_length=20, max_length=64)
    memory_id: str | None = Field(default=None, min_length=1, max_length=256)
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    expected_revision: str | None = Field(default=None, min_length=1, max_length=256)

class SharedMemoryReadRequest(SharedMemoryPolicyPreflightRequest):
    """Explicit, feature-gated governed read; writes remain unavailable."""

    operation: Literal[SharedOperation.READ] = SharedOperation.READ
    memory_id: str = Field(min_length=1, max_length=256)

class MemoryCrystallizeRequest(BaseModel):
    source_ids: list[str]
    project: str
    title: str
    summary: str
    target_type: str = "pattern"
    concepts: list[str] | None = None
    files: list[str] | None = None
    upsert_key: str | None = None

class FactSynthesisThreeZoneContext(BaseModel):
    Active: list[str] = Field(default_factory=list)
    Compress: list[str] = Field(default_factory=list)
    Frozen: list[str] = Field(default_factory=list)

class FactSynthesisRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_name: str
    session_id: str
    three_zone_context: FactSynthesisThreeZoneContext
    importance_score: int | None = Field(default=None, ge=1, le=10)


class FactCrystal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    core_insight: str
    root_cause_resolved: str
    reusable_patterns: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    importance_score: int = Field(default=5, ge=1, le=10)
    linked_dependencies: list[dict[str, str]] = Field(default_factory=list)
    domain: FactCrystalDomain
    priority: FactCrystalPriority
    semantic_type: FactCrystalSemanticType

class CheckpointCreateRequest(BaseModel):
    project: str
    checkpoint_type: str = "workflow"
    title: str | None = None
    content: str | None = None
    done: str = ""
    next: str = ""
    checks: str = ""
    risks: str = ""
    concepts: list[str] | None = None
    files: list[str] | None = None
    upsert_key: str | None = None

class ProjectMapUpsertRequest(BaseModel):
    project: str
    title: str | None = None
    auth: str = ""
    routing: str = ""
    tests: str = ""
    deploy: str = ""
    i18n: str = ""
    websocket: str = ""
    risks: str = ""
    notes: str = ""
    files: list[str] | None = None
    concepts: list[str] | None = None
    upsert_key: str | None = None

class MemoryMergeRequest(BaseModel):
    project: str
    source_id: str
    target_id: str
    archive_source: bool = True

class MemoryDetectRequest(BaseModel):
    project: str | None = None
    limit: int = 20
    include_archived: bool = False

class MemoryLintRequest(BaseModel):
    id: str
    project: str | None = None

class MemoryDeleteRequest(BaseModel):
    id: str
    project: str | None = None

class MemoryConfidenceRequest(BaseModel):
    id: str
    project: str | None = None
    confidence: float

class MemoryPinRequest(BaseModel):
    id: str
    project: str | None = None
    pinned: bool = True

class MemoryVoteRequest(BaseModel):
    id: str
    project: str | None = None
    vote: int
    voter: str = "agent"

class MemorySourceRefsRequest(BaseModel):
    id: str
    project: str | None = None
    refs: list[str]

class AdrCreateRequest(BaseModel):
    project: str
    title: str
    context: str = ""
    decision: str = ""
    consequences: str = ""
    status: str = "accepted"
    files: list[str] | None = None
    concepts: list[str] | None = None
    upsert_key: str | None = None

class AdrSupersedeRequest(BaseModel):
    project: str
    old_id: str
    new_id: str

class HandoffCreateRequest(BaseModel):
    project: str
    title: str
    current_state: str = ""
    decisions: str = ""
    validation: str = ""
    next_agent_action: str = ""
    next_owner_id: str = ""
    handoff_sla_deadline: str = ""
    files: list[str] | None = None
    concepts: list[str] | None = None
    upsert_key: str | None = None

class SessionRecordCreateRequest(BaseModel):
    project: str
    title: str
    done: str = ""
    next: str = ""
    checks: str = ""
    risks: str = ""
    decisions: str = ""
    files_touched: list[str] | None = None
    conversation_notes: str = ""
    transcript_ref: str = ""
    upsert_key: str | None = None

class TaskOpenRequest(BaseModel):
    project: str = "e-github-workspace"
    task_id: str = Field(min_length=1, max_length=256)
    intent: str = Field(min_length=1, max_length=8000)
    title: str = ""
    scope_in: list[str] = Field(default_factory=list)
    scope_out: list[str] = Field(default_factory=list)
    repo: str = ""
    owner: str = ""
    session_id: str = ""
    correlation_id: str = ""
    files_touched: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    upsert_key: str | None = None

class TaskCloseRequest(BaseModel):
    project: str = "e-github-workspace"
    task_id: str = Field(min_length=1, max_length=256)
    done: str = ""
    next: str = ""
    checks: str = ""
    risks: str = ""
    decisions: str = ""
    validation: str = ""
    files_touched: list[str] | None = None
    conversation_notes: str = ""
    transcript_ref: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

class TaskIngressControlRequest(BaseModel):
    """Bounded operator reason for an in-memory task-ingress transition."""

    reason: str = Field(min_length=1, max_length=240)

class TaskContextUpdateRequest(BaseModel):
    project: str
    title: str = "active-task"
    current_task: str = ""
    status: str = ""
    pending_items: str = ""
    guidance: str = ""
    next_step: str = ""
    files_touched: list[str] | None = None
    upsert_key: str | None = None

class RiskRegisterUpdateRequest(BaseModel):
    project: str
    title: str = "risk-register"
    summary: str = ""
    top_risks: list[str] | None = None
    mitigations: list[str] | None = None
    owner: str = ""
    upsert_key: str | None = None

class ValidationSnapshotSaveRequest(BaseModel):
    project: str
    title: str = "validation-snapshot"
    lint: str = ""
    tests: str = ""
    smoke: str = ""
    docs: str = ""
    overall_status: str = ""
    command_summary: str = ""
    upsert_key: str | None = None

class MemoryTimelineRequest(BaseModel):
    project: str | None = None
    concept: str | None = None
    memory_type: str | None = None
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    as_of: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    include_temporal_unknown: bool = False
    include_archived: bool = False
    limit: int = 20

class HardDeleteMemoryRequest(BaseModel):
    id: str
    project: str | None = None

class BatchUpsertMemoriesRequest(BaseModel):
    project: str | None = None
    items: list[BatchMemoryUpsertItem]

class BatchLinkMemoriesRequest(BaseModel):
    project: str | None = None
    items: list[BatchMemoryLinkItem]

class BatchAttachSourceRefsRequest(BaseModel):
    project: str | None = None
    items: list[dict]

class SourceRefsReplaceRequest(BaseModel):
    id: str
    project: str | None = None
    refs: list[str]

class SourceRefsDetachRequest(BaseModel):
    id: str
    project: str | None = None
    refs: list[str]

class RestoreMemoryRequest(BaseModel):
    id: str
    project: str | None = None

class BatchMemoryIdsRequest(BaseModel):
    project: str | None = None
    items: list[dict]

class RepairLiveIndexesRequest(BaseModel):
    project: str | None = None
    aggregate: bool = False
    remove_orphan_links: bool = True
    remove_orphan_artifacts: bool = False

class RebuildProjectSummaryRequest(BaseModel):
    project: str
    upsert_key: str | None = None

class ProjectSummaryListRequest(BaseModel):
    project: str | None = None
    limit: int = 20
    offset: int = 0

class EntityExtractRequest(BaseModel):
    id: str
    project: str | None = None

class RelationSuggestRequest(BaseModel):
    project: str | None = None
    limit: int = 20

class MemoryCompactRequest(BaseModel):
    id: str
    project: str | None = None
    summary: str

class PolicyGuardRequest(BaseModel):
    content: str
    project: str | None = None
    memory_type: str | None = None

class MemoryDiffRequest(BaseModel):
    left_id: str
    right_id: str
    project: str | None = None

class SearchByRefRequest(BaseModel):
    ref: str
    project: str | None = None
    limit: int = 20

class SearchByUpsertKeyRequest(BaseModel):
    upsert_key: str
    project: str | None = None

class BatchRestoreRequest(BaseModel):
    project: str | None = None
    items: list[dict]

class ArtifactRestoreRequest(BaseModel):
    artifact_type: str
    artifact_id: str
    project: str | None = None

class OrphanArtifactRelinkRequest(BaseModel):
    artifact_type: str
    artifact_id: str
    target_memory_id: str
    project: str | None = None

class MemoryStalenessReportRequest(BaseModel):
    project: str | None = None
    days: int = 30
    limit: int = 20

class MemoryReviewQueueRequest(BaseModel):
    project: str | None = None
    limit: int = 20
    include_conflicts: bool = True
    include_closed: bool = False

class MemoryTriageQueueRequest(BaseModel):
    project: str | None = None
    limit: int = 20
    include_closed: bool = False

class ProjectSummaryRefreshAllRequest(BaseModel):
    project: str | None = None
    projects: list[str] | None = None
    aggregate: bool = False

class RelationApplySuggestionsRequest(BaseModel):
    project: str | None = None
    aggregate: bool = False
    min_score: float = 0.65
    limit: int = 20
    include_relates_to: bool = False

class MemoryMergePreviewRequest(BaseModel):
    project: str
    source_id: str
    target_id: str

class SchemaUpgradeAllRequest(BaseModel):
    project: str | None = None
    aggregate: bool = False

class MemoryRedactRequest(BaseModel):
    id: str
    project: str | None = None
    patterns: list[StrictStr] | None = Field(default=None, max_length=_CUSTOM_REDACTION_MAX_PATTERNS)
    replacement: StrictStr = Field(default="[REDACTED]", max_length=120)

class SecretScanRequest(BaseModel):
    project: str | None = None
    limit: int = 50

class RelationConfidenceRequest(BaseModel):
    source_id: str
    target_id: str
    relation: str
    project: str
    confidence: float

class RelationVoteRequest(BaseModel):
    source_id: str
    target_id: str
    relation: str
    project: str
    vote: int
    voter: str = "agent"

class MemoryAliasRequest(BaseModel):
    id: str
    alias: str
    project: str | None = None

class AliasResolveRequest(BaseModel):
    alias: str
    project: str | None = None

class EntityCatalogRequest(BaseModel):
    project: str | None = None
    aggregate: bool = False

class ProjectSummaryCompareRequest(BaseModel):
    left_project: str
    right_project: str

class RecentFailuresFeedRequest(BaseModel):
    project: str | None = None
    limit: int = 20

class ProjectOnlyRequest(BaseModel):
    project: str | None = None

class ScopedMaintenanceRequest(BaseModel):
    project: str | None = None
    aggregate: bool = False

class ProjectRetirementRequest(BaseModel):
    project: StrictStr = Field(min_length=1)
    capability: StrictStr = ""
    backup_dir: StrictStr | None = None

class ProjectSummaryPinRequest(BaseModel):
    project: str

class MemorySchemaValidateRequest(BaseModel):
    id: str
    project: str | None = None

class ArtifactDeleteRequest(BaseModel):
    artifact_type: str
    artifact_id: str
    project: str | None = None
    delete_backing_memory: bool = False

class ArtifactListRequest(BaseModel):
    artifact_type: str
    project: str | None = None
    limit: int = 20
    offset: int = 0

class MemoryGcCandidatesRequest(BaseModel):
    project: str | None = None
    stale_days: int = 90
    limit: int = 20

class MemoryCompactionReportRequest(BaseModel):
    project: str | None = None
    min_chars: int = 1200
    min_lines: int = 25
    limit: int = 20

class LinkCycleDetectRequest(BaseModel):
    project: str | None = None
    limit: int = 20

class ProjectMapCompareRequest(BaseModel):
    left_project: str
    right_project: str

class ValidationTrendReportRequest(BaseModel):
    project: str
    limit: int = 20

class EntitySearchRequest(BaseModel):
    query: str
    project: str | None = None
    limit: int = 20

class EntityLinkMemoriesRequest(BaseModel):
    entity: str
    project: str
    relation: str = "relates_to"
    limit: int = 20

class RelationPruneLowQualityRequest(BaseModel):
    project: str | None = None
    aggregate: bool = False
    max_confidence: float = 0.5
    max_quality_score: float = 2.5
    remove_unscored: bool = False

class ProjectSimilarityReportRequest(BaseModel):
    project: str
    limit: int = 10

class MemoryChangelogRequest(BaseModel):
    id: str
    project: str | None = None
    limit: int = 50

class ReviewQueueApplyRequest(BaseModel):
    project: str | None = None
    limit: int = 20
    mark_needs_review: bool = True
    auto_redact_secrets: bool = True
    queue_ids: list[str] = Field(default_factory=list, max_length=50)
    status: Literal["needs_review", "resolved", "dismissed"] = "needs_review"

class TriageQueueApplyRequest(BaseModel):
    project: str | None = None
    limit: int = 20
    min_score: float = 0.75
    include_relates_to: bool = False

class HardDeleteRestorePreviewRequest(BaseModel):
    id: str
    project: str | None = None

class ArtifactBatchDeleteRequest(BaseModel):
    artifact_type: str
    artifact_ids: list[str]
    project: str | None = None
    delete_backing_memory: bool = False

class ArtifactBatchRelinkRequest(BaseModel):
    artifact_type: str
    items: list[dict]
    project: str | None = None

class ArtifactBatchRestoreRequest(BaseModel):
    artifact_type: str
    artifact_ids: list[str]
    project: str | None = None

class StrictSchemaValidateRequest(BaseModel):
    project: str | None = None
    include_archived: bool = True

class IntegrityRepairStrictRequest(BaseModel):
    project: str | None = None
    aggregate: bool = False
    remove_orphan_links: bool = True
    remove_orphan_artifacts: bool = True
    normalize_metadata: bool = True

class AdminExportRequest(BaseModel):
    project: str | None = None
    include_archived: bool = True
    include_artifacts: bool = True
    export_name: str | None = None

class AdminImportPreviewRequest(BaseModel):
    path: str
    project: str | None = None

class AdminImportApplyRequest(BaseModel):
    path: str
    merge_mode: str = "upsert"
    project: str | None = None

class PolicyProfileSetRequest(BaseModel):
    max_content_chars: int = 8000
    max_lines: int = 120
    require_project: bool = True
    require_memory_type: bool = False
    block_secret_like: bool = True
    block_raw_logs: bool = False

class PolicyEnforceMemoryRequest(BaseModel):
    id: str
    project: str | None = None
    auto_redact: bool = False

class OverlapReportRequest(BaseModel):
    project: str | None = None
    limit: int = 20

class OverlapCleanupApplyRequest(BaseModel):
    project: str | None = None
    aggregate: bool = False
    limit: int = 20
    archive_sources: bool = True

class TypeMigrateRequest(BaseModel):
    id: str
    project: str | None = None
    new_type: str

class HybridSearchRequest(BaseModel):
    query: str
    project: str | None = None
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    as_of: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    include_temporal_unknown: bool = False
    domain: str | None = None
    semantic_type: str | None = None
    priority: str | None = None
    include_archived: bool = False
    include_logs: bool = False
    limit: int = 10

class BhmMatchSearchRequest(BaseModel):
    query: str
    limit: int = 5
    project: str | None = None
    domain: str | None = None
    semantic_type: str | None = None
    priority: str | None = None
    include_archived: bool = False
    include_logs: bool = False

class ReflectRequest(BaseModel):
    project: str = "e-github-workspace"
    maxClusters: int = 10

class SlotRequest(BaseModel):
    label: str
    content: str
    sizeLimit: int = 2000
    description: str = ""
    pinned: bool = True
    scope: str = "project"
    project: str = "e-github-workspace"

class SlotAppendRequest(BaseModel):
    label: str
    text: str
    project: str = "e-github-workspace"

class SlotReplaceRequest(BaseModel):
    label: str
    content: str
    project: str = "e-github-workspace"

class SlotLabelRequest(BaseModel):
    label: str
    project: str = "e-github-workspace"

class LessonRequest(BaseModel):
    content: str
    context: str = ""
    confidence: float = 0.7
    project: str = "e-github-workspace"
    tags: list[str] = []

class LessonStrengthenRequest(BaseModel):
    lessonId: str
    project: str = "e-github-workspace"

class MemoryVerifyRequest(BaseModel):
    id: str
    project: str = "e-github-workspace"

class BhmHookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    schemaVersion: Literal["1.0"] = "1.0"
    eventId: StrictStr | None = None
    hookType: StrictStr = Field(min_length=1)
    sessionId: StrictStr = Field(min_length=1)
    correlationId: StrictStr | None = None
    parentEventId: StrictStr | None = None
    project: StrictStr = Field(min_length=1)
    cwd: StrictStr = Field(default="")
    timestamp: StrictStr | None = None
    source: StrictStr = "hook"
    payloadState: Literal["raw", "sanitized"] = "raw"
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

class BhmHookCompactRequest(BhmHookRequest):
    source_ids: list[StrictStr] | None = None
    transit_buffer: list[Any] | None = None
    title: StrictStr | None = None
    summary: StrictStr | None = None
    target_type: StrictStr = "pattern"
    concepts: list[StrictStr] | None = None
    files: list[StrictStr] | None = None
    upsert_key: StrictStr | None = None

class BhmHookIdleRequest(BhmHookRequest):
    duplicate_limit: StrictInt = Field(default=20, ge=1, le=100)
    decay_limit: StrictInt = Field(default=200, ge=1, le=500)
    reflection_limit: StrictInt = Field(default=40, ge=1, le=200)
    reflection_scan_limit: StrictInt = Field(default=120, ge=1, le=500)
    reflection_timeout: StrictInt = Field(default=20, ge=1, le=120)
    max_orphans: StrictInt = Field(default=25, ge=1, le=200)
    max_links: StrictInt = Field(default=60, ge=1, le=300)
    apply_reflection: StrictBool = False
    apply_graph_healer: StrictBool = True
