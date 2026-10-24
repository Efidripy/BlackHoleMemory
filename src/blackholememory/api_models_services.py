from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .code_graph_query import ALLOWED_OPERATIONS as CODE_GRAPH_QUERY_OPERATIONS
from .context_compiler import MAX_CONTEXT_TOKEN_BUDGET
from .documentation_factory import DOCUMENTATION_FACTORY_MAX_DOCUMENTS
from .factory_integration import FACTORY_INTEGRATION_MAX_ITEMS
from .llm_candidates import LLM_CANDIDATE_MAX
from .llm_code_fabric import LLM_CODE_FABRIC_TASKS
from .llm_learning import LLM_LEARNING_MAX_DATASET_RECORDS
from .memory_graph import MEMORY_GRAPH_OPERATIONS
from .qa_incident_factory import QA_INCIDENT_MAX_ARTIFACTS
from .repository_intelligence import REPOSITORY_INTELLIGENCE_MAX_FILES
from .resource_limits import BHM_INDEX_MAX_FILES_PER_RUN
from .retrieval_lab import RETRIEVAL_LAB_MAX_BENCHMARK_CASES
from .session_capture import DISCLOSURE_LEVELS
from .task_graph import TASK_GRAPH_OPERATIONS


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
