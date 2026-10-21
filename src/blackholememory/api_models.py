from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .config import settings
from .memory_contracts import MemoryMetadata, MemoryClass, MemoryEventRole
from .context_compiler import MAX_CONTEXT_TOKEN_BUDGET
from .resource_limits import BHM_INDEX_MAX_FILES_PER_RUN
from .governed_shared_memory import SharedOperation, SharedVisibility
from .utility_feedback import UtilityEventType
from .llm_candidates import LLM_CANDIDATE_MAX
from .retrieval_lab import RETRIEVAL_LAB_MAX_BENCHMARK_CASES
from .repository_intelligence import REPOSITORY_INTELLIGENCE_MAX_FILES
from .code_graph_query import ALLOWED_OPERATIONS as CODE_GRAPH_QUERY_OPERATIONS
from .session_capture import DISCLOSURE_LEVELS
from .memory_graph import MEMORY_GRAPH_OPERATIONS
from .task_graph import TASK_GRAPH_OPERATIONS
from .llm_code_fabric import LLM_CODE_FABRIC_TASKS
from .factory_integration import FACTORY_INTEGRATION_MAX_ITEMS
from .qa_incident_factory import QA_INCIDENT_MAX_ARTIFACTS
from .documentation_factory import DOCUMENTATION_FACTORY_MAX_DOCUMENTS
from .llm_learning import LLM_LEARNING_MAX_DATASET_RECORDS

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

class LLMJobSubmitRequest(BaseModel):
    """Bounded, proposal-only request for asynchronous local-LLM delegation."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: StrictStr = Field(min_length=1, max_length=200)
    job_type: StrictStr = Field(min_length=1, max_length=96)
    payload: dict[str, Any] = Field(default_factory=dict)
    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    priority: StrictInt = Field(default=100, ge=-1_000, le=1_000)
    workload: Literal["interactive", "foreground", "background"] = "foreground"
    max_wall_seconds: float = Field(default=120.0, gt=0, le=3_600)
    max_output_tokens: StrictInt = Field(default=512, ge=1, le=4_096)
    max_attempts: StrictInt = Field(default=3, ge=1, le=10)

class LLMCandidatePlanRequest(BaseModel):
    """Bounded objective for an execution-free multi-candidate plan."""

    model_config = ConfigDict(extra="forbid")

    task_id: StrictStr = Field(min_length=1, max_length=200)
    objective: Any = Field(default_factory=dict)
    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    roles: list[StrictStr] | None = None
    candidate_count: StrictInt = Field(default=4, ge=1, le=LLM_CANDIDATE_MAX)
    prompt_version: StrictStr = Field(default="candidate-v1", min_length=1, max_length=120)
    model_digest: StrictStr = Field(default="local-model", min_length=1, max_length=160)

class LLMDelegationDecisionRequest(BaseModel):
    """Bounded workload facts for an explainable local-first decision."""

    model_config = ConfigDict(extra="forbid")

    task_type: StrictStr = Field(min_length=1, max_length=96)
    confidence: float = Field(ge=0.0, le=1.0)
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    mutation_requested: StrictBool = False
    evidence_count: StrictInt = Field(default=0, ge=0, le=1000)
    local_capabilities: list[StrictStr] | None = None
    risk_flags: list[StrictStr] | None = None
    operator_approved: StrictBool = False

class MemoryFoundryPreviewRequest(BaseModel):
    """Bounded read-only consolidation request for the local LLM contour."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    memory_ids: list[StrictStr] | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=32, ge=1, le=96)
    stale_days: StrictInt = Field(default=90, ge=1, le=3650)
    undo_window_seconds: StrictInt = Field(default=900, ge=1, le=86_400)

class RetrievalLabPreviewRequest(BaseModel):
    """Bounded retrieval experiment request; model execution is never started."""

    model_config = ConfigDict(extra="forbid")

    query: StrictStr = Field(min_length=1, max_length=480)
    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    candidates: list[dict[str, Any]] | None = Field(default=None, max_length=128)
    feature_flags: dict[str, StrictBool] | None = None
    limit: StrictInt = Field(default=10, ge=1, le=50)
    benchmark_cases: StrictInt = Field(default=8, ge=1, le=RETRIEVAL_LAB_MAX_BENCHMARK_CASES)
    latency_budget_ms: float = Field(default=250.0, gt=0, le=10_000)
    use_live_candidates: StrictBool = True

class RepositoryIntelligencePreviewRequest(BaseModel):
    """Bounded repository analysis request with no source mutation authority."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    root: StrictStr = Field(default=".", min_length=1, max_length=240)
    paths: list[StrictStr] | None = Field(default=None, max_length=64)
    files: list[dict[str, Any]] | None = Field(default=None, max_length=64)
    changed_paths: list[StrictStr] = Field(default_factory=list, max_length=64)
    include_tests: StrictBool = True
    max_files: StrictInt = Field(default=REPOSITORY_INTELLIGENCE_MAX_FILES, ge=1, le=REPOSITORY_INTELLIGENCE_MAX_FILES)

class CodeGraphQueryRequest(BaseModel):
    """Bounded internal WI-03 graph query; never writes or returns source."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    root_id: StrictStr | None = Field(default=None, max_length=160)
    snapshot_id: StrictStr | None = Field(default=None, max_length=160)
    operation: Literal[tuple(sorted(CODE_GRAPH_QUERY_OPERATIONS))] = "symbol"
    query: StrictStr = Field(default="", max_length=480)
    depth: StrictInt = Field(default=2, ge=0, le=8)
    limit: StrictInt = Field(default=32, ge=1, le=128)
    max_tokens: StrictInt = Field(default=4_096, ge=128, le=16_384)
    time_budget_ms: float = Field(default=250.0, ge=1.0, le=5_000.0)

class PublicCodeToolRequest(BaseModel):
    """Unified bounded public MCP code-tools request.

    The request is intentionally a single allowlisted contract.  It never
    accepts arbitrary SQL/Cypher or source payloads and defaults to a
    read-only status/plan operation; indexing requires explicit ``apply``.
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "index",
        "status",
        "projects",
        "watch",
        "search",
        "code_search",
        "code_snippet",
        "graph_artifact_export",
        "graph_artifact_verify",
        "graph_artifact_promotion_plan",
        "graph_query",
        "graph",
        "schema",
        "coverage",
        "architecture",
        "trace",
        "trace_evidence",
        "impact",
        "impact_preview",
        "cross_repo",
        "package_resolution",
        "dependency_provenance",
        "type_references",
        "bicep_module_resolution",
    ]
    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    root: StrictStr | None = Field(default=None, min_length=1, max_length=512)
    apply: StrictBool = False
    build_graph: StrictBool = True
    defer_graph: StrictBool = False
    graph_only: StrictBool = False
    force_refresh: StrictBool = False
    max_files_per_run: StrictInt = Field(default=BHM_INDEX_MAX_FILES_PER_RUN, ge=1, le=BHM_INDEX_MAX_FILES_PER_RUN)
    expected_job_id: StrictStr | None = Field(default=None, min_length=1, max_length=120)
    expected_state_digest: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    query: StrictStr = Field(default="", max_length=480)
    graph_operation: Literal[tuple(sorted(CODE_GRAPH_QUERY_OPERATIONS))] = "symbol"
    edge_kinds: list[StrictStr] = Field(default_factory=list, max_length=16)
    name_pattern: StrictStr | None = Field(default=None, max_length=120)
    path_pattern: StrictStr | None = Field(default=None, max_length=240)
    label: StrictStr | None = Field(default=None, max_length=40)
    min_degree: StrictInt | None = Field(default=None, ge=0, le=10_000)
    max_degree: StrictInt | None = Field(default=None, ge=0, le=10_000)
    depth: StrictInt = Field(default=2, ge=0, le=8)
    limit: StrictInt = Field(default=32, ge=1, le=128)
    offset: StrictInt = Field(default=0, ge=0, le=10_000)
    max_tokens: StrictInt = Field(default=4_096, ge=128, le=16_384)
    time_budget_ms: float = Field(default=250.0, ge=1.0, le=5_000.0)
    snapshot_id: StrictStr | None = Field(default=None, max_length=160)
    expected_graph_digest: StrictStr | None = Field(default=None, max_length=128)
    changed_paths: list[StrictStr] = Field(default_factory=list, max_length=64)
    search_mode: Literal["text", "path", "symbol", "metadata"] = "text"
    include_snippets: StrictBool = False
    snippet_max_chars: StrictInt = Field(default=280, ge=80, le=600)
    path: StrictStr | None = Field(default=None, max_length=512)
    line: StrictInt = Field(default=1, ge=1, le=1_000_000)
    context: StrictInt = Field(default=2, ge=0, le=8)
    artifact_path: StrictStr | None = Field(default=None, max_length=1024)
    detached_signature_b64: StrictStr | None = Field(default=None, max_length=16_384)
    detached_public_key_b64: StrictStr | None = Field(default=None, max_length=16_384)
    adoption_receipt_digest: StrictStr | None = Field(default=None, max_length=128)
    rollback_anchor_snapshot_id: StrictStr | None = Field(default=None, max_length=160)
    rollback_anchor_digest: StrictStr | None = Field(default=None, max_length=128)
    base_revision: StrictStr | None = Field(default=None, max_length=64)
    include_git_history: StrictBool = True
    semantic_fusion: StrictBool = False
    semantic_weight: float = Field(default=0.35, ge=0.0, le=0.75)
    semantic_query: list[StrictStr] | None = Field(default=None, max_length=32)
    semantic_min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    cycles: StrictInt = Field(default=1, ge=1, le=10)
    interval_seconds: float = Field(default=0.0, ge=0.0, le=300.0)
    debounce_seconds: float = Field(default=0.0, ge=0.0, le=30.0)

class ConventionMemoryPreviewRequest(BaseModel):
    """Bounded internal WI-04 convention preview; never returns source."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    root_id: StrictStr | None = Field(default=None, max_length=160)
    graph_snapshot_id: StrictStr | None = Field(default=None, max_length=160)

class ChangeImpactPreviewRequest(BaseModel):
    """Bounded internal WI-34 change-impact/edit-preflight preview."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    root_id: StrictStr | None = Field(default=None, max_length=160)
    graph_snapshot_id: StrictStr | None = Field(default=None, max_length=160)
    expected_graph_digest: StrictStr | None = Field(default=None, max_length=128)
    changed_paths: list[StrictStr] = Field(default_factory=list, max_length=64)

class UnifiedContextCompileRequest(BaseModel):
    """Hidden WI-08 source-aware context compiler; public MCP remains unchanged."""

    model_config = ConfigDict(extra="forbid")

    query: StrictStr = Field(min_length=1, max_length=480)
    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    code_operation: Literal[tuple(sorted(CODE_GRAPH_QUERY_OPERATIONS))] = "symbol"
    include_code: StrictBool = True
    include_conventions: StrictBool = True
    include_proposals: StrictBool = False
    limit: StrictInt = Field(default=16, ge=1, le=32)
    token_budget: StrictInt = Field(default=1_200, ge=64, le=MAX_CONTEXT_TOKEN_BUDGET)
    time_budget_ms: float = Field(default=500.0, ge=1.0, le=5_000.0)

class SessionCapturePreviewRequest(BaseModel):
    """Hidden WI-05 bounded session capture/progressive-disclosure request."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    session_id: StrictStr | None = Field(default=None, max_length=160)
    disclosure: Literal[tuple(DISCLOSURE_LEVELS)] = "standard"
    token_budget: StrictInt = Field(default=1_200, ge=64, le=16_384)
    max_items: StrictInt = Field(default=32, ge=1, le=64)
    stale_days: StrictInt = Field(default=90, ge=1, le=3_650)
    undo_window_seconds: StrictInt = Field(default=900, ge=1, le=604_800)

class MemoryGraphQueryRequest(BaseModel):
    """Hidden WI-06 bounded temporal graph query; read-only."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    operation: Literal[tuple(MEMORY_GRAPH_OPERATIONS)] = "as_of"
    query: StrictStr = Field(default="", max_length=240)
    snapshot_id: StrictStr | None = Field(default=None, max_length=160)
    as_of: StrictStr | None = Field(default=None, max_length=64)
    depth: StrictInt = Field(default=2, ge=0, le=8)
    limit: StrictInt = Field(default=32, ge=1, le=128)
    max_tokens: StrictInt = Field(default=4_096, ge=128, le=16_384)
    time_budget_ms: float = Field(default=500.0, gt=0.0, le=5_000.0)

class TaskGraphQueryRequest(BaseModel):
    """Hidden WI-07 bounded task-governance query; read-only."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    operation: Literal[tuple(TASK_GRAPH_OPERATIONS)] = "status"
    query: StrictStr = Field(default="", max_length=240)
    snapshot_id: StrictStr | None = Field(default=None, max_length=160)
    limit: StrictInt = Field(default=64, ge=1, le=128)
    max_tokens: StrictInt = Field(default=4_096, ge=128, le=16_384)
    time_budget_ms: float = Field(default=500.0, gt=0.0, le=5_000.0)

class LLMCodeFabricPlanRequest(BaseModel):
    """Hidden WI-09 proposal-only local LLM code-fabric plan request."""

    model_config = ConfigDict(extra="forbid")

    task_type: Literal[tuple(LLM_CODE_FABRIC_TASKS)]
    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    context_digest: StrictStr = Field(default="", max_length=64)
    required_capabilities: list[StrictStr] = Field(default_factory=lambda: ["json"], max_length=8)
    context_tokens: StrictInt = Field(default=8_192, ge=1, le=131_072)
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    mutation_requested: StrictBool = False
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    evidence_count: StrictInt = Field(default=1, ge=0, le=1_000)
    risk_flags: list[StrictStr] = Field(default_factory=list, max_length=16)
    operator_approved: StrictBool = False

class FactoryIntegrationPreviewRequest(BaseModel):
    """Hidden WI-10 QA/incident/docs integration preview request."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    artifacts: list[dict[str, Any]] = Field(default_factory=list, max_length=FACTORY_INTEGRATION_MAX_ITEMS)
    documents: list[dict[str, Any]] = Field(default_factory=list, max_length=FACTORY_INTEGRATION_MAX_ITEMS)
    changed_paths: list[StrictStr] = Field(default_factory=list, max_length=64)
    code_items: list[dict[str, Any]] = Field(default_factory=list, max_length=FACTORY_INTEGRATION_MAX_ITEMS)
    task_items: list[dict[str, Any]] = Field(default_factory=list, max_length=FACTORY_INTEGRATION_MAX_ITEMS)
    risk_class: Literal["low", "medium", "high", "critical"] = "medium"
    max_items: StrictInt = Field(default=32, ge=1, le=FACTORY_INTEGRATION_MAX_ITEMS)

class UnifiedMcpContractPreviewRequest(BaseModel):
    """Hidden WI-11 unified MCP/hooks/adapters contract preview."""

    model_config = ConfigDict(extra="forbid")

    manifest_path: StrictStr | None = Field(default=None, max_length=480)
    initialize_response: dict[str, Any] | None = None
    catalog_response: dict[str, Any] | None = None
    client_snapshots: list[dict[str, Any]] | None = Field(default=None, max_length=8)
    native_mcp: dict[str, Any] | None = None
    hook_profile: dict[str, Any] | None = None

class CapabilityRoutePreviewRequest(BaseModel):
    """Hidden WI-13 proposal-only capability route request."""

    model_config = ConfigDict(extra="forbid")

    task_type: StrictStr = Field(min_length=1, max_length=80)
    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    scope: StrictStr = Field(default="repository", min_length=1, max_length=240)
    required_capabilities: list[StrictStr] | None = Field(default=None, max_length=8)
    context_tokens: StrictInt = Field(default=8_192, ge=1, le=131_072)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    mutation_requested: StrictBool = False
    evidence_count: StrictInt = Field(default=1, ge=0, le=1_000)
    risk_flags: list[StrictStr] = Field(default_factory=list, max_length=16)
    operator_approved: StrictBool = False
    local_capabilities: list[StrictStr] | None = Field(default=None, max_length=8)
    measurements: list[dict[str, Any]] | None = Field(default=None, max_length=8)
    models: list[dict[str, Any]] | None = Field(default=None, max_length=16)
    claim_state: dict[str, Any] | None = None

class HumanUiBridgePreviewRequest(BaseModel):
    """Hidden WI-12 bounded human UI and Obsidian preview request."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr | None = Field(default=None, max_length=120)
    nodes: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    links: list[dict[str, Any]] = Field(default_factory=list, max_length=512)
    selected_id: StrictStr | None = Field(default=None, max_length=200)
    provenance: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    review_items: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    task_items: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    context_packet: dict[str, Any] | None = None
    mcp_state: dict[str, Any] | None = None
    obsidian_export: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    obsidian_import: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    snapshot_id: StrictStr = Field(default="", max_length=160)
    generated_at: StrictStr = Field(default="", max_length=80)

class MigrationPreviewRequest(BaseModel):
    """Hidden WI-14 dry-run migration/compatibility request."""

    model_config = ConfigDict(extra="forbid")

    records: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    source_kind: StrictStr = Field(default="generic", max_length=80)
    source_url: StrictStr = Field(default="", max_length=240)
    source_commit: StrictStr = Field(default="", max_length=160)
    source_license: StrictStr = Field(default="", max_length=120)
    input_schema: StrictStr = Field(default="generic.v1", max_length=80)
    reviewer: StrictStr = Field(default="", max_length=120)
    approved_licenses: list[StrictStr] = Field(default_factory=list, max_length=16)
    project: StrictStr | None = Field(default=None, max_length=120)
    dry_run: StrictBool = True

class SecurityTrustBoundaryPreviewRequest(BaseModel):
    """Hidden WI-15 fail-closed security and trust-boundary preview request."""

    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    source_kind: StrictStr = Field(default="memory", max_length=80)
    source_url: StrictStr = Field(default="", max_length=240)
    source_commit: StrictStr = Field(default="", max_length=160)
    source_license: StrictStr = Field(default="", max_length=120)
    reviewer: StrictStr = Field(default="", max_length=120)
    project_roots: list[StrictStr] = Field(default_factory=list, max_length=64)
    paths: list[StrictStr] = Field(default_factory=list, max_length=64)
    mcp_endpoints: list[StrictStr] = Field(default_factory=list, max_length=32)
    proposed_actions: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    route: StrictStr = Field(default="/bhm/security/trust-boundary/preview", max_length=240)
    method: StrictStr = Field(default="POST", max_length=16)
    capability: StrictStr | None = Field(default=None, max_length=256)
    mutation_requested: StrictBool = False
    operator_approved: StrictBool = False
    feature_enabled: StrictBool = False
    max_items: StrictInt = Field(default=64, ge=1, le=64)

class QAIncidentPreviewRequest(BaseModel):
    """Bounded QA/incident evidence request; no tests or models are started."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    artifacts: list[dict[str, Any]] = Field(default_factory=list, max_length=QA_INCIDENT_MAX_ARTIFACTS)
    changed_paths: list[StrictStr] = Field(default_factory=list, max_length=64)
    release_candidate: dict[str, Any] | None = None
    feature_flags: dict[str, StrictBool] | None = None
    max_items: StrictInt = Field(default=32, ge=1, le=64)

class DocumentationFactoryPreviewRequest(BaseModel):
    """Bounded documentation/ops/vision proposal request."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    documents: list[dict[str, Any]] = Field(default_factory=list, max_length=DOCUMENTATION_FACTORY_MAX_DOCUMENTS)
    locale: StrictStr = Field(default="ru-RU", min_length=2, max_length=32)
    vision_confirmed: StrictBool = False
    vision_assets: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    feature_flags: dict[str, StrictBool] | None = None
    max_patches: StrictInt = Field(default=32, ge=1, le=96)

class NightShiftPreviewRequest(BaseModel):
    """Dry-run Night Shift planning request."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    jobs: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    resource_snapshot: dict[str, Any] = Field(default_factory=dict)
    maintenance_window_open: StrictBool = False
    user_active: StrictBool = False
    dry_run: StrictBool = True
    max_jobs: StrictInt = Field(default=32, ge=1, le=64)

class ModelRouterDecisionRequest(BaseModel):
    """Capability/profile facts for a fail-closed local model route."""

    model_config = ConfigDict(extra="forbid")

    task_type: StrictStr = Field(min_length=1, max_length=96)
    required_capabilities: list[StrictStr] = Field(default_factory=list, max_length=8)
    context_tokens: StrictInt = Field(default=8192, ge=1, le=131_072)
    measurements: list[dict[str, Any]] | None = Field(default=None, max_length=8)
    models: list[dict[str, Any]] | None = Field(default=None, max_length=16)

class LLMCachePreviewRequest(BaseModel):
    """Digest-only cache/prefix policy request; no cache write is performed."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    content: Any = Field(default_factory=dict)
    prompt: StrictStr = Field(min_length=1, max_length=64_000)
    prompt_prefix: StrictStr | None = Field(default=None, max_length=4_096)
    prompt_version: StrictStr = Field(default="default-v1", min_length=1, max_length=120)
    model_digest: StrictStr = Field(default="local-model", min_length=1, max_length=160)
    parameters: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    result_supplied: StrictBool = False
    inspect_store: StrictBool = False
    prefix_limit: StrictInt = Field(default=8, ge=1, le=32)

class LLMLearningReviewRequest(BaseModel):
    """Explicit human-reviewed outcome for the local-LLM learning loop."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    source_job_id: StrictStr = Field(min_length=1, max_length=200)
    decision: Literal["accepted", "rejected"]
    reviewer: StrictStr = Field(min_length=1, max_length=160)
    review_reason: StrictStr = Field(min_length=1, max_length=2_000)
    input: Any = Field(default_factory=dict)
    prompt: StrictStr = Field(min_length=1, max_length=64_000)
    output: Any = Field(default_factory=dict)
    prompt_version: StrictStr = Field(default="default-v1", min_length=1, max_length=120)
    model_digest: StrictStr = Field(default="local-model", min_length=1, max_length=160)
    parameters: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)

class LLMLearningCurateRequest(BaseModel):
    """Build a reviewed dataset proposal without training or persistence."""

    model_config = ConfigDict(extra="forbid")

    project: StrictStr = Field(default="blackholememory", min_length=1, max_length=120)
    limit: StrictInt = Field(default=LLM_LEARNING_MAX_DATASET_RECORDS, ge=1, le=LLM_LEARNING_MAX_DATASET_RECORDS)
    include_payload: StrictBool = True

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
