from __future__ import annotations

import asyncio
import copy  # noqa: F401
import json
import os  # noqa: F401
import sqlite3  # noqa: F401
import subprocess  # noqa: F401
import threading  # noqa: F401
import time
import urllib.request  # noqa: F401
from contextlib import asynccontextmanager
from datetime import (
    datetime,  # noqa: F401
    timedelta,  # noqa: F401
    timezone,  # noqa: F401
)
from pathlib import Path  # noqa: F401
from typing import Any

from fastapi import (
    FastAPI,
    HTTPException,  # noqa: F401
    Request,
)
from fastapi.responses import JSONResponse
from starlette.routing import Route

from . import (
    app_core,
    app_core_fallback,
    app_core_hit,
    app_core_hook,
    app_governed,
    app_graphs,
    app_hooks,
    app_lifecycle,
    app_mcp,
    app_ops,
    app_ops_admin,
    app_ops_task,
    app_ops_task2,
    app_projects,
    app_retrieval,
    app_telemetry,
    code_tools_service,
    galaxy_routes,
    graph_build_jobs,  # noqa: F401
    llm_routes,
    memory_routes,
    ui_routes,
)
from . import app_runtime as rt
from .api_models import (
    AdminExportRequest,  # noqa: F401
    AdminImportApplyRequest,  # noqa: F401
    AdminImportPreviewRequest,  # noqa: F401
    AdrCreateRequest,  # noqa: F401
    AdrSupersedeRequest,  # noqa: F401
    AgentContextRequest,  # noqa: F401
    AliasResolveRequest,  # noqa: F401
    ArtifactBatchDeleteRequest,  # noqa: F401
    ArtifactBatchRelinkRequest,  # noqa: F401
    ArtifactBatchRestoreRequest,  # noqa: F401
    ArtifactDeleteRequest,  # noqa: F401
    ArtifactListRequest,  # noqa: F401
    ArtifactRestoreRequest,  # noqa: F401
    BatchAttachSourceRefsRequest,  # noqa: F401
    BatchLinkMemoriesRequest,  # noqa: F401
    BatchMemoryIdsRequest,  # noqa: F401
    BatchMemoryLinkItem,  # noqa: F401
    BatchMemoryUpsertItem,  # noqa: F401
    BatchRestoreRequest,  # noqa: F401
    BatchUpsertMemoriesRequest,  # noqa: F401
    BhmHookCompactRequest,  # noqa: F401
    BhmHookIdleRequest,  # noqa: F401
    BhmHookRequest,  # noqa: F401
    BhmMatchSearchRequest,  # noqa: F401
    CapabilityRoutePreviewRequest,  # noqa: F401
    ChangeImpactPreviewRequest,  # noqa: F401
    CheckpointCreateRequest,  # noqa: F401
    CodeGraphQueryRequest,  # noqa: F401
    ConsolidationChangeSetPreviewRequest,  # noqa: F401
    ConsolidationChangeSetReviewRequest,  # noqa: F401
    ContextCompileRequest,  # noqa: F401
    ContextTierPromotionRollbackRequest,  # noqa: F401
    ConventionMemoryPreviewRequest,  # noqa: F401
    DocumentationFactoryPreviewRequest,  # noqa: F401
    EntityCatalogRequest,  # noqa: F401
    EntityExtractRequest,  # noqa: F401
    EntityLinkMemoriesRequest,  # noqa: F401
    EntitySearchRequest,  # noqa: F401
    FactCrystal,  # noqa: F401
    FactCrystalDomain,  # noqa: F401
    FactCrystalPriority,  # noqa: F401
    FactCrystalSemanticType,  # noqa: F401
    FactoryIntegrationPreviewRequest,  # noqa: F401
    FactSynthesisRequest,  # noqa: F401
    FactSynthesisThreeZoneContext,  # noqa: F401
    ForgetApplyRequest,  # noqa: F401
    ForgetPreviewRequest,  # noqa: F401
    GalaxyDataLink,  # noqa: F401
    GalaxyDataNode,  # noqa: F401
    GalaxyDataResponse,  # noqa: F401
    GalaxyStatsResponse,  # noqa: F401
    GovernedConsolidationApplyRequest,  # noqa: F401
    GovernedConsolidationCreateRequest,  # noqa: F401
    GovernedConsolidationDecisionRequest,  # noqa: F401
    GovernedConsolidationProposalRequest,  # noqa: F401
    GovernedSemanticProposalRequest,  # noqa: F401
    HandoffCreateRequest,  # noqa: F401
    HardDeleteMemoryRequest,  # noqa: F401
    HardDeleteRestorePreviewRequest,  # noqa: F401
    HumanUiBridgePreviewRequest,  # noqa: F401
    HybridSearchRequest,  # noqa: F401
    IntegrityRepairStrictRequest,  # noqa: F401
    LauncherStatsResponse,  # noqa: F401
    LessonRequest,  # noqa: F401
    LessonStrengthenRequest,  # noqa: F401
    LinkCycleDetectRequest,  # noqa: F401
    LLMCachePreviewRequest,  # noqa: F401
    LLMCandidatePlanRequest,  # noqa: F401
    LLMCodeFabricPlanRequest,  # noqa: F401
    LLMDelegationDecisionRequest,  # noqa: F401
    LLMJobSubmitRequest,  # noqa: F401
    LLMLearningCurateRequest,  # noqa: F401
    LLMLearningReviewRequest,  # noqa: F401
    McpRepairRequest,  # noqa: F401
    MemoryAdvancedSearchRequest,  # noqa: F401
    MemoryAliasRequest,  # noqa: F401
    MemoryArchiveRequest,  # noqa: F401
    MemoryChangelogRequest,  # noqa: F401
    MemoryCompactionReportRequest,  # noqa: F401
    MemoryCompactRequest,  # noqa: F401
    MemoryConfidenceRequest,  # noqa: F401
    MemoryCrystallizeRequest,  # noqa: F401
    MemoryDeleteRequest,  # noqa: F401
    MemoryDetectRequest,  # noqa: F401
    MemoryDiffRequest,  # noqa: F401
    MemoryFoundryPreviewRequest,  # noqa: F401
    MemoryGcCandidatesRequest,  # noqa: F401
    MemoryGraphQueryRequest,  # noqa: F401
    MemoryLinkDeleteRequest,  # noqa: F401
    MemoryLinkRequest,  # noqa: F401
    MemoryLintRequest,  # noqa: F401
    MemoryMergePreviewRequest,  # noqa: F401
    MemoryMergeRequest,  # noqa: F401
    MemoryPinRequest,  # noqa: F401
    MemoryRecentActivityRequest,  # noqa: F401
    MemoryRedactRequest,  # noqa: F401
    MemoryReviewQueueRequest,  # noqa: F401
    MemorySchemaValidateRequest,  # noqa: F401
    MemorySourceRefsRequest,  # noqa: F401
    MemoryStalenessReportRequest,  # noqa: F401
    MemoryTimelineRequest,  # noqa: F401
    MemoryTriageQueueRequest,  # noqa: F401
    MemoryUpdateRequest,  # noqa: F401
    MemoryUpsertRequest,  # noqa: F401
    MemoryUsedRequest,  # noqa: F401
    MemoryVerifyRequest,  # noqa: F401
    MemoryVoteRequest,  # noqa: F401
    MigrationPreviewRequest,  # noqa: F401
    ModelRouterDecisionRequest,  # noqa: F401
    NightShiftPreviewRequest,  # noqa: F401
    OrphanArtifactRelinkRequest,  # noqa: F401
    OverlapCleanupApplyRequest,  # noqa: F401
    OverlapReportRequest,  # noqa: F401
    PolicyEnforceMemoryRequest,  # noqa: F401
    PolicyGuardRequest,  # noqa: F401
    PolicyProfileSetRequest,  # noqa: F401
    ProjectMapCompareRequest,  # noqa: F401
    ProjectMapUpsertRequest,  # noqa: F401
    ProjectOnlyRequest,  # noqa: F401
    ProjectRetirementRequest,  # noqa: F401
    ProjectSimilarityReportRequest,  # noqa: F401
    ProjectSummaryCompareRequest,  # noqa: F401
    ProjectSummaryListRequest,  # noqa: F401
    ProjectSummaryPinRequest,  # noqa: F401
    ProjectSummaryRefreshAllRequest,  # noqa: F401
    PublicCodeToolRequest,  # noqa: F401
    QAIncidentPreviewRequest,  # noqa: F401
    RebuildProjectSummaryRequest,  # noqa: F401
    RecentFailuresFeedRequest,  # noqa: F401
    ReflectRequest,  # noqa: F401
    RelationApplySuggestionsRequest,  # noqa: F401
    RelationConfidenceRequest,  # noqa: F401
    RelationPruneLowQualityRequest,  # noqa: F401
    RelationSuggestRequest,  # noqa: F401
    RelationVoteRequest,  # noqa: F401
    RememberRequest,  # noqa: F401
    RepairLiveIndexesRequest,  # noqa: F401
    RepositoryIntelligencePreviewRequest,  # noqa: F401
    RestoreMemoryRequest,  # noqa: F401
    RetrievalExplainRequest,  # noqa: F401
    RetrievalLabPreviewRequest,  # noqa: F401
    ReviewQueueApplyRequest,  # noqa: F401
    RiskRegisterUpdateRequest,  # noqa: F401
    SchemaUpgradeAllRequest,  # noqa: F401
    ScopedMaintenanceRequest,  # noqa: F401
    SearchByRefRequest,  # noqa: F401
    SearchByUpsertKeyRequest,  # noqa: F401
    SearchRequest,  # noqa: F401
    SecretScanRequest,  # noqa: F401
    SecurityTrustBoundaryPreviewRequest,  # noqa: F401
    SessionCapturePreviewRequest,  # noqa: F401
    SessionRecordCreateRequest,  # noqa: F401
    SharedMemoryPolicyPreflightRequest,  # noqa: F401
    SharedMemoryReadRequest,  # noqa: F401
    SlotAppendRequest,  # noqa: F401
    SlotLabelRequest,  # noqa: F401
    SlotReplaceRequest,  # noqa: F401
    SlotRequest,  # noqa: F401
    SourceRefsDetachRequest,  # noqa: F401
    SourceRefsReplaceRequest,  # noqa: F401
    StrictSchemaValidateRequest,  # noqa: F401
    TaskCloseRequest,  # noqa: F401
    TaskContextUpdateRequest,  # noqa: F401
    TaskGraphQueryRequest,  # noqa: F401
    TaskIngressControlRequest,  # noqa: F401
    TaskOpenRequest,  # noqa: F401
    TriageQueueApplyRequest,  # noqa: F401
    TypeMigrateRequest,  # noqa: F401
    UiSessionExchangeRequest,  # noqa: F401
    UnifiedContextCompileRequest,  # noqa: F401
    UnifiedMcpContractPreviewRequest,  # noqa: F401
    UtilityFeedbackEventRequest,  # noqa: F401
    ValidationSnapshotSaveRequest,  # noqa: F401
    ValidationTrendReportRequest,  # noqa: F401
)  # noqa: F401
from .app_runtime import (
    _MAX_CONCURRENT_WRITES,  # noqa: F401
    ResponseTimeout,
    _env_float,  # noqa: F401
    _run_bounded_read,  # noqa: F401
    _safe_exception_text,
    _write_json_atomic,  # noqa: F401
)  # noqa: F401
from .architecture_intelligence import (
    build_architecture_explain_receipt,  # noqa: F401
    build_architecture_intelligence,  # noqa: F401
    build_architecture_memory,  # noqa: F401
    build_graph_analysis_quality_receipt,  # noqa: F401
)
from .caller_auth import (
    MAX_PROJECT_INSPECTION_BYTES,
    CallerRoutePolicy,
    authorize_project_root,  # noqa: F401
    authorize_projects,
    caller_auth_configuration_error,
    caller_project_scope_requires_explicit,
    caller_route_policy,
    configured_caller_principal,
    extract_request_projects,
    is_caller_token_valid,
    parse_bearer_token,
)
from .capability import (
    ADMIN_CAPABILITY_HEADER,
    admin_route_requires_capability,
    is_admin_capability_valid,
)
from .change_impact import (
    build_change_impact_preview,  # noqa: F401
    collect_git_change_paths,  # noqa: F401
    collect_git_history_stats,  # noqa: F401
)
from .code_tools_service import (
    _PUBLIC_CODE_TOOL_OPERATIONS,  # noqa: F401
    _get_or_start_graph_build_operation,  # noqa: F401
    _graph_build_job_store,  # noqa: F401
    _graph_build_operation_id,  # noqa: F401
    _graph_build_result_from_snapshot,  # noqa: F401
    _latest_graph_build_operation,  # noqa: F401
    _public_code_contract_digest,  # noqa: F401
    _public_code_embedding_contract,  # noqa: F401
    _public_code_projects,  # noqa: F401
    _public_code_request_context,  # noqa: F401
    _public_code_root_matches_configured_root,  # noqa: F401
    _public_graph_build_operation,  # noqa: F401
    _resolve_bounded_repository_root,  # noqa: F401
    _resolve_implicit_public_code_root,  # noqa: F401
    _resolve_public_code_root,  # noqa: F401
    _run_graph_build_operation,  # noqa: F401
    _shutdown_graph_parse_executor,  # noqa: F401
    bhm_public_code_tools,  # noqa: F401
)
from .config import settings
from .context_compiler import MAX_CONTEXT_ITEM_CHARS  # noqa: F401
from .context_tier_promotion import rollback_tier_promotion  # noqa: F401
from .context_tier_promotion import (
    runtime_enabled as context_tier_promotion_enabled,  # noqa: F401
)
from .exact_identifier_retrieval import exact_identifier_enabled  # noqa: F401
from .galaxy_routes import (
    _atlas_cache,  # noqa: F401
    _galaxy_label_from_text,  # noqa: F401
    _galaxy_memory_collection_names,  # noqa: F401
    _galaxy_memory_record_node,  # noqa: F401
    _galaxy_node_color,  # noqa: F401
    _galaxy_node_value,  # noqa: F401
    _galaxy_payload_is_active,  # noqa: F401
    _galaxy_payload_text,  # noqa: F401
    _galaxy_point_node,  # noqa: F401
    _galaxy_string_list,  # noqa: F401
    _load_galaxy_active_nodes,  # noqa: F401
    _load_galaxy_active_nodes_sync,  # noqa: F401
    _load_galaxy_authoritative_nodes_sync,  # noqa: F401
    _load_galaxy_code_project_nodes,  # noqa: F401
    _load_galaxy_code_project_nodes_sync,  # noqa: F401
    _merge_galaxy_node,  # noqa: F401
    bhm_galaxy_atlas,  # noqa: F401
    bhm_galaxy_classic_view,  # noqa: F401
    bhm_galaxy_data,  # noqa: F401
    bhm_galaxy_stats,  # noqa: F401
    bhm_galaxy_view,  # noqa: F401
)
from .governed_consolidation import GovernedConsolidationRepository  # noqa: F401
from .governed_consolidation import (
    runtime_enabled as governed_consolidation_enabled,  # noqa: F401
)
from .governed_consolidation import (
    runtime_status as governed_consolidation_status,  # noqa: F401
)
from .governed_semantic_editor import (
    GovernedSemanticEditorUnavailable,  # noqa: F401
    LocalGatewaySemanticCompletion,  # noqa: F401
    build_semantic_proposal,  # noqa: F401
)
from .graph import build_graph  # noqa: F401
from .health import dependency_report  # noqa: F401
from .llm_gateway import LocalOpenAICompatibleAdapter  # noqa: F401
from .llm_routes import (
    _LLM_CACHE_STORE,  # noqa: F401
    _LLM_JOB_QUEUE_CAPACITY,  # noqa: F401
    _LLM_LONG_TASK_STORE,  # noqa: F401
    _llm_public_job,  # noqa: F401
    _repository_intelligence_scope,  # noqa: F401
    bhm_llm_cache_preview,  # noqa: F401
    bhm_llm_cache_status,  # noqa: F401
    bhm_llm_cancel_job,  # noqa: F401
    bhm_llm_candidate_plan,  # noqa: F401
    bhm_llm_capabilities,  # noqa: F401
    bhm_llm_code_fabric_plan,  # noqa: F401
    bhm_llm_delegation_decide,  # noqa: F401
    bhm_llm_documentation_factory_preview,  # noqa: F401
    bhm_llm_job_result,  # noqa: F401
    bhm_llm_job_status,  # noqa: F401
    bhm_llm_learning_curate,  # noqa: F401
    bhm_llm_learning_review,  # noqa: F401
    bhm_llm_learning_status,  # noqa: F401
    bhm_llm_memory_foundry_preview,  # noqa: F401
    bhm_llm_model_router_decide,  # noqa: F401
    bhm_llm_model_router_snapshot,  # noqa: F401
    bhm_llm_night_shift_preview,  # noqa: F401
    bhm_llm_qa_incident_preview,  # noqa: F401
    bhm_llm_repository_intelligence_preview,  # noqa: F401
    bhm_llm_retrieval_lab_preview,  # noqa: F401
    bhm_llm_submit_job,  # noqa: F401
)
from .local_endpoint_policy import open_local_url  # noqa: F401
from .mcp_repair import (
    build_repair_preview,  # noqa: F401
    build_reprobe,  # noqa: F401
)
from .mcp_surfaces import CORE_TOOL_NAMES  # noqa: F401
from .mem0_adapter import (
    StorageNotReady,
    _remote_qdrant_available,  # noqa: F401
    ensure_memory_collections,
    get_global_core_memory,  # noqa: F401
    get_project_mem0_memory,  # noqa: F401
    get_qdrant_client,  # noqa: F401
    global_collection_name,  # noqa: F401
    lexical_score,  # noqa: F401
    local_collection_name,  # noqa: F401
    mem0_runtime_plan,  # noqa: F401
    storage_runtime_state,
)
from .memory_doctor import (
    load_authoritative_sqlite_snapshot,  # noqa: F401
    run_authoritative_sqlite_memory_doctor,  # noqa: F401
)
from .memory_pulse_bus import MemoryPulseBus  # noqa: F401
from .memory_routes import (
    _access_updates_for_hits,  # noqa: F401
    _add_semantic_dependency_links,  # noqa: F401
    _alias_resolve,  # noqa: F401
    _attach_source_refs,  # noqa: F401
    _batch_archive_memories,  # noqa: F401
    _batch_attach_source_refs,  # noqa: F401
    _batch_delete_memories,  # noqa: F401
    _batch_link_memories,  # noqa: F401
    _batch_restore_memories,  # noqa: F401
    _batch_unlink_memories,  # noqa: F401
    _batch_upsert_memories,  # noqa: F401
    _compact_memory,  # noqa: F401
    _delete_memory_link,  # noqa: F401
    _detach_source_refs,  # noqa: F401
    _get_memory_links,  # noqa: F401
    _get_source_refs,  # noqa: F401
    _lint_memory,  # noqa: F401
    _list_archived_memories,  # noqa: F401
    _list_live_memories,  # noqa: F401
    _list_pinned_memories,  # noqa: F401
    _memory_alias_add,  # noqa: F401
    _memory_alias_remove,  # noqa: F401
    _memory_changelog,  # noqa: F401
    _memory_compaction_report,  # noqa: F401
    _memory_diff,  # noqa: F401
    _memory_gc_candidates,  # noqa: F401
    _memory_merge_preview,  # noqa: F401
    _memory_restore_hard_deleted_preview,  # noqa: F401
    _memory_schema_validate,  # noqa: F401
    _memory_staleness_report,  # noqa: F401
    _memory_timeline,  # noqa: F401
    _memory_triage_queue,  # noqa: F401
    _memory_type_migrate,  # noqa: F401
    _memory_usage_stats,  # noqa: F401
    _replace_source_refs,  # noqa: F401
    _secret_scan_existing_memories,  # noqa: F401
    _serialize_duplicate_candidate,  # noqa: F401
    _serialized_link_mutation,  # noqa: F401
    _set_memory_confidence,  # noqa: F401
    _update_vector_access_payloads,  # noqa: F401
    _update_vector_access_payloads_async,  # noqa: F401
    _vote_memory_quality,  # noqa: F401
    bhm_alias_resolve,  # noqa: F401
    bhm_memories_archived,  # noqa: F401
    bhm_memories_batch_archive,  # noqa: F401
    bhm_memories_batch_delete,  # noqa: F401
    bhm_memories_batch_link,  # noqa: F401
    bhm_memories_batch_unlink,  # noqa: F401
    bhm_memories_batch_upsert,  # noqa: F401
    bhm_memories_by_concept,  # noqa: F401
    bhm_memories_by_type,  # noqa: F401
    bhm_memories_list,  # noqa: F401
    bhm_memories_pinned,  # noqa: F401
    bhm_memory_alias_add,  # noqa: F401
    bhm_memory_alias_remove,  # noqa: F401
    bhm_memory_archive,  # noqa: F401
    bhm_memory_changelog,  # noqa: F401
    bhm_memory_compact,  # noqa: F401
    bhm_memory_compaction_report,  # noqa: F401
    bhm_memory_delete_hard,  # noqa: F401
    bhm_memory_detect_conflicts,  # noqa: F401
    bhm_memory_detect_duplicates,  # noqa: F401
    bhm_memory_diff,  # noqa: F401
    bhm_memory_gc_candidates,  # noqa: F401
    bhm_memory_link_create,  # noqa: F401
    bhm_memory_link_delete,  # noqa: F401
    bhm_memory_links_get,  # noqa: F401
    bhm_memory_lint,  # noqa: F401
    bhm_memory_merge,  # noqa: F401
    bhm_memory_merge_preview,  # noqa: F401
    bhm_memory_normalize_metadata,  # noqa: F401
    bhm_memory_pin,  # noqa: F401
    bhm_memory_redact,  # noqa: F401
    bhm_memory_restore,  # noqa: F401
    bhm_memory_restore_batch,  # noqa: F401
    bhm_memory_restore_hard_deleted_preview,  # noqa: F401
    bhm_memory_review_queue,  # noqa: F401
    bhm_memory_schema_validate,  # noqa: F401
    bhm_memory_set_confidence,  # noqa: F401
    bhm_memory_source_refs_attach,  # noqa: F401
    bhm_memory_source_refs_batch,  # noqa: F401
    bhm_memory_source_refs_detach,  # noqa: F401
    bhm_memory_source_refs_get,  # noqa: F401
    bhm_memory_source_refs_replace,  # noqa: F401
    bhm_memory_staleness_report,  # noqa: F401
    bhm_memory_timeline,  # noqa: F401
    bhm_memory_triage_queue,  # noqa: F401
    bhm_memory_type_migrate,  # noqa: F401
    bhm_memory_update,  # noqa: F401
    bhm_memory_upsert,  # noqa: F401
    bhm_memory_usage_stats,  # noqa: F401
    bhm_memory_used,  # noqa: F401
    bhm_memory_vote_quality,  # noqa: F401
    bhm_secret_scan_existing_memories,  # noqa: F401
)
from .observation_security import (
    OBSERVATION_COMPACT_MAX_INPUT_BYTES,  # noqa: F401
    OBSERVATION_IDLE_MAX_INPUT_BYTES,  # noqa: F401
    OBSERVATION_MAX_INPUT_BYTES,  # noqa: F401
    observation_body_limit,
)
from .openapi_contract import build_openapi_schema
from .projection_generation_binding import resolve_projection_route  # noqa: F401
from .repository_index import (
    index_repository,  # noqa: F401
    repository_index_status,  # noqa: F401
)
from .repository_intelligence import collect_repository_files  # noqa: F401
from .resource_limits import (
    BHM_CODE_COVERAGE_PROBE_TIMEOUT_SECONDS,  # noqa: F401
    BHM_CODE_GRAPH_SOFT_WAIT_SECONDS,  # noqa: F401
    BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS,  # noqa: F401
    BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS,  # noqa: F401
    BHM_FEDERATED_EMBEDDING_PREPARATION_TIMEOUT_SECONDS,  # noqa: F401
    BHM_FEDERATED_RETRIEVAL_CONTOUR_TIMEOUT_SECONDS,  # noqa: F401
    BHM_INDEX_MAX_FILES_PER_RUN,  # noqa: F401
    LLM_HTTP_TIMEOUT_SECONDS,  # noqa: F401
    PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS,  # noqa: F401
    PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS,  # noqa: F401
    PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS,  # noqa: F401
    PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS,  # noqa: F401
    QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS,  # noqa: F401
    SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS,  # noqa: F401
)
from .runtime_endpoints import validate_loopback_listener_host
from .runtime_storage import MemoryStoreMode, resolve_runtime_storage_config
from .shared_memory_audit import append_shared_memory_audit  # noqa: F401
from .sqlite_retention import SQLiteRetentionPolicy, automatic_sqlite_retention_loop
from .storage_state import qdrant_required_for_core
from .surface_report import build_surface_report
from .sync_service import (
    InvalidTombstone,  # noqa: F401
    UndoWindowExpired,  # noqa: F401
)
from .temporal_contract import temporal_contract_enabled  # noqa: F401
from .typed_memory_contract import typed_memory_capability_available  # noqa: F401
from .ui_routes import (
    _UI_SESSIONS,  # noqa: F401
    _ui_browser_request_is_same_origin,  # noqa: F401
    _ui_request_is_loopback,  # noqa: F401
    bhm_human_ui_preview,  # noqa: F401
    bhm_ui_boot_report,  # noqa: F401
    bhm_ui_code_tools,  # noqa: F401
    bhm_ui_session_bootstrap,  # noqa: F401
    bhm_ui_session_exchange,  # noqa: F401
    bhm_ui_session_mint,  # noqa: F401
    bhm_ui_session_renew,  # noqa: F401
    bhm_ui_session_status,  # noqa: F401
)
from .ui_session import (
    BOOTSTRAP_TTL_SECONDS,  # noqa: F401
    SESSION_TTL_SECONDS,  # noqa: F401
    UI_SESSION_COOKIE,
    UiSessionRegistry,  # noqa: F401
    ui_session_route_allowed,
)
from .usage_telemetry import monotonic_elapsed_ms
from .utility_feedback_store import (
    append_utility_event,  # noqa: F401
    load_utility_events,  # noqa: F401
)

if not rt._PROVIDER_WARMUP_REQUIRED:
    rt._PROVIDER_WARMUP_READY.set()


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    validate_loopback_listener_host(settings.host)
    _UI_SESSIONS.reset()
    caller_configuration_error = caller_auth_configuration_error()
    if caller_configuration_error:
        raise RuntimeError(
            f"BHM caller authentication is not configured: {caller_configuration_error}"
        )
    memory_store = rt._memory_store_state()
    if memory_store.configured_mode == MemoryStoreMode.SQLITE_AUTHORITATIVE.value and not memory_store.ready:
        raise RuntimeError(
            "sqlite-authoritative memory mode is not ready: "
            f"{memory_store.reason}"
        )
    sqlite_authoritative = memory_store.configured_mode == MemoryStoreMode.SQLITE_AUTHORITATIVE.value
    if sqlite_authoritative:
        # The repository's first write intentionally runs an integrity-complete
        # SQLite initializer.  Complete that bounded gate before readiness is
        # advertised so the first MCP mutation cannot commit after the default
        # client deadline without returning its receipt.
        await asyncio.to_thread(app_lifecycle._initialize_authoritative_memory_service)
    storage = await asyncio.to_thread(storage_runtime_state)
    projection_required = qdrant_required_for_core() or not sqlite_authoritative
    if projection_required:
        waited_storage = await app_lifecycle._wait_for_required_storage_ready()
        if waited_storage is not None:
            storage = waited_storage
    if storage.ready:
        collection_report = await asyncio.to_thread(ensure_memory_collections, settings.qdrant_collection)
        print(
            "[INFO] BHM Qdrant contours ready: "
            f"local={collection_report['local']['collection_name']} "
            f"global={collection_report['global']['collection_name']}",
            flush=True,
        )
    else:
        print(
            "[WARN] BHM started with SQLite authoritative and Qdrant projection degraded: "
            f"{storage.reason}",
            flush=True,
        )
    warmup_task: asyncio.Task[None] | None = None
    boot_report_task: asyncio.Task[None] | None = None
    telemetry_task: asyncio.Task[None] | None = None
    sqlite_retention_task: asyncio.Task[None] | None = None
    try:
        warmup_task = asyncio.create_task(rt.warmup_provider_probe())
        if app_lifecycle._boot_report_is_pending():
            boot_report_task = asyncio.create_task(app_lifecycle._finalize_pending_boot_report(warmup_task))
        telemetry_task = asyncio.create_task(app_lifecycle._telemetry_harvester_loop())
        if sqlite_authoritative and settings.sqlite_retention_enabled:
            retention_database = resolve_runtime_storage_config(
                runtime_dir=settings.runtime_dir
            ).database_path
            sqlite_retention_task = asyncio.create_task(
                automatic_sqlite_retention_loop(
                    retention_database,
                    SQLiteRetentionPolicy(
                        keep_graph_history_per_scope=settings.sqlite_retention_keep_graph_history,
                        keep_index_history_per_scope=settings.sqlite_retention_keep_index_history,
                        keep_completed_outbox=settings.sqlite_retention_keep_completed_outbox,
                        keep_latest_completed_outbox_per_aggregate=(
                            settings.sqlite_retention_keep_latest_outbox_per_aggregate
                        ),
                        graph_min_age_days=settings.sqlite_retention_graph_min_age_days,
                        index_min_age_days=settings.sqlite_retention_index_min_age_days,
                        completed_outbox_min_age_days=settings.sqlite_retention_outbox_min_age_days,
                        max_graph_snapshots_per_run=(
                            settings.sqlite_retention_max_graph_snapshots_per_cycle
                        ),
                        max_index_snapshots_per_run=(
                            settings.sqlite_retention_max_index_snapshots_per_cycle
                        ),
                        max_completed_outbox_per_run=(
                            settings.sqlite_retention_max_outbox_events_per_cycle
                        ),
                    ),
                    initial_delay_seconds=settings.sqlite_retention_initial_delay_seconds,
                    interval_seconds=settings.sqlite_retention_interval_seconds,
                )
            )
        await app_lifecycle._start_hook_queue_workers()
        async with app_core._MCP_STREAMABLE_HTTP.run():
            yield
    finally:
        _UI_SESSIONS.reset()
        try:
            await app_lifecycle._stop_hook_queue_workers()
        finally:
            await app_lifecycle._cancel_lifespan_task(boot_report_task, name="boot_report")
            await app_lifecycle._cancel_lifespan_task(telemetry_task, name="telemetry")
            await app_lifecycle._cancel_lifespan_task(sqlite_retention_task, name="sqlite_retention")
            await app_lifecycle._cancel_lifespan_task(warmup_task, name="provider_warmup")
            await app_lifecycle._cleanup_registered_infra_processes(reason="api_shutdown")
            _shutdown_graph_parse_executor()


app = FastAPI(title=settings.app_name, redoc_url=None, lifespan=_app_lifespan)
app.router.routes.append(
    Route(
        "/mcp",
        endpoint=app_core._MCP_STREAMABLE_HTTP.asgi_app,
        methods=["GET", "POST", "DELETE"],
        name="bhm-streamable-http-mcp",
    )
)


@app.exception_handler(StorageNotReady)
async def storage_not_ready_handler(_request: Request, exc: StorageNotReady) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": {"code": "storage_not_ready", "reason": _safe_exception_text(exc)}},
    )


@app.middleware("http")
async def caller_and_admin_capability_guard(request: Request, call_next):
    if request.url.path == "/bhm/ui/session/exchange" and request.method.upper() == "POST":
        raw_content_length = request.headers.get("content-length")
        if not raw_content_length:
            return JSONResponse(
                status_code=411,
                content={"detail": {"code": "ui_bootstrap_content_length_required"}},
            )
        try:
            content_length = int(raw_content_length)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"detail": {"code": "invalid_content_length"}},
            )
        if content_length < 0:
            return JSONResponse(
                status_code=400,
                content={"detail": {"code": "invalid_content_length"}},
            )
        if content_length > app_lifecycle.MAX_UI_EXCHANGE_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": {"code": "ui_bootstrap_payload_too_large"}},
            )
        raw_body = await request.body()
        if len(raw_body) > app_lifecycle.MAX_UI_EXCHANGE_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": {"code": "ui_bootstrap_payload_too_large"}},
            )

    policy = caller_route_policy(request.url.path, request.method)
    if policy is not CallerRoutePolicy.EXEMPT:
        configured_principal = configured_caller_principal()
        if configured_principal is None:
            return JSONResponse(
                status_code=503,
                content={"detail": {"code": "caller_auth_not_configured"}},
            )
        supplied = parse_bearer_token(request.headers.get("authorization"))
        # /mcp alone also accepts the operator capability as the bearer (clients that forward only Authorization).
        accepted = is_caller_token_valid(supplied) or (request.url.path == "/mcp" and is_admin_capability_valid(supplied))
        principal = configured_principal if accepted else None
        auth_kind = "caller_bearer" if principal is not None else ""
        if principal is None and ui_session_route_allowed(request.url.path, request.method):
            session_candidate = request.cookies.get(UI_SESSION_COOKIE)
            session_principal = _UI_SESSIONS.resolve_session(
                session_candidate,
                expected_principal=configured_principal,
            )
            if session_principal is not None and _ui_browser_request_is_same_origin(
                request,
                require_origin=request.method.upper() not in {"GET", "HEAD"},
            ):
                principal = session_principal
                auth_kind = "ui_session"
        if principal is None:
            if request.url.path == "/bhm/ui/session/bootstrap":
                if not _ui_request_is_loopback(request) or not _ui_browser_request_is_same_origin(request):
                    return JSONResponse(
                        status_code=403,
                        headers={"Cache-Control": "no-store"},
                        content={"detail": {"code": "ui_direct_bootstrap_loopback_only"}},
                    )
                return JSONResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                    content={"detail": {"code": "ui_launcher_bootstrap_required"}},
                )
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                content={"detail": {"code": "caller_auth_required"}},
            )

        project_sources: list[Any] = [request.query_params]
        explicit_project_scope = caller_project_scope_requires_explicit(
            request.url.path,
            request.method,
        )
        if (
            policy is CallerRoutePolicy.PROJECT
            and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
            and (not principal.all_projects or explicit_project_scope)
        ):
            raw_content_length = request.headers.get("content-length")
            transfer_encoding = str(request.headers.get("transfer-encoding") or "").casefold()
            if not raw_content_length and "chunked" in {
                item.strip() for item in transfer_encoding.split(",") if item.strip()
            }:
                return JSONResponse(
                    status_code=411,
                    content={"detail": {"code": "caller_scope_content_length_required"}},
                )
            if raw_content_length:
                try:
                    content_length = int(raw_content_length)
                except ValueError:
                    return JSONResponse(
                        status_code=400,
                        content={"detail": {"code": "invalid_content_length"}},
                    )
                if content_length < 0:
                    return JSONResponse(
                        status_code=400,
                        content={"detail": {"code": "invalid_content_length"}},
                    )
                if content_length > MAX_PROJECT_INSPECTION_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": {"code": "caller_scope_payload_too_large"}},
                    )
            try:
                raw_body = await request.body()
                if len(raw_body) > MAX_PROJECT_INSPECTION_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": {"code": "caller_scope_payload_too_large"}},
                    )
                if raw_body:
                    project_sources.append(json.loads(raw_body))
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                pass
        if policy is CallerRoutePolicy.PROJECT:
            requested_projects = extract_request_projects(*project_sources)
            if explicit_project_scope and not requested_projects:
                return JSONResponse(
                    status_code=403,
                    content={"detail": {"code": "caller_project_required"}},
                )
            project_error = authorize_projects(
                principal,
                requested_projects,
                require_explicit=(
                    explicit_project_scope or not principal.all_projects
                ),
            )
            if project_error:
                return JSONResponse(
                    status_code=403,
                    content={"detail": {"code": project_error}},
                )
        request.state.bhm_caller_principal = principal
        request.state.bhm_auth_kind = auth_kind

    if admin_route_requires_capability(request.url.path, request.method):
        candidate = request.headers.get(ADMIN_CAPABILITY_HEADER, "")
        if not is_admin_capability_valid(candidate):
            return JSONResponse(
                status_code=403,
                content={"detail": {"code": "admin_capability_required"}},
            )
    return await call_next(request)


@app.middleware("http")
async def observation_content_length_guard(request: Request, call_next):
    limit = observation_body_limit(request.url.path)
    if limit is not None:
        raw_content_length = request.headers.get("content-length")
        if raw_content_length:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": {"code": "invalid_content_length"}},
                )
            if content_length < 0:
                return JSONResponse(
                    status_code=400,
                    content={"detail": {"code": "invalid_content_length"}},
                )
            if content_length > limit:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": {
                            "code": "observation_payload_too_large",
                            "stage": "content-length",
                            "actualBytes": content_length,
                            "limitBytes": limit,
                        }
                    },
                )
    return await call_next(request)


@app.middleware("http")
async def usage_telemetry_guard(request: Request, call_next):
    started_at = time.perf_counter()
    response = None
    status = "exception"
    timed_out = False
    try:
        response = await call_next(request)
        status = app_lifecycle._rest_usage_status(int(response.status_code))
        return response
    except (asyncio.TimeoutError, TimeoutError, ResponseTimeout):
        status = "timeout"
        timed_out = True
        raise
    except Exception:
        status = "exception"
        raise
    finally:
        app_core._USAGE_TELEMETRY.record(
            surface=app_core_hook._request_surface(request),
            operation=app_lifecycle._rest_usage_operation(request),
            status=status,
            duration_ms=monotonic_elapsed_ms(started_at),
            response_size_bytes=app_lifecycle._response_size_from_headers(response),
            timeout=timed_out,
        )


app_ops.register(app)


app_telemetry.register(app)


ui_routes.register(app)


llm_routes.register(app)


app_graphs.register(app)


code_tools_service.register(app)


app_retrieval.register(app)


app_hooks.register(app)


app_mcp.register(app)


@app.get("/bhm/telemetry/surface-report")
async def bhm_surface_report() -> dict[str, Any]:
    from . import bhm_mcp

    tools = await bhm_mcp.mcp.list_tools()
    return build_surface_report(
        mcp_tools=tools,
        public_openapi=build_openapi_schema(app, "public"),
        admin_openapi=build_openapi_schema(app, "admin"),
        usage_snapshot=app_core._USAGE_TELEMETRY.snapshot(),
    )


galaxy_routes.register(app)


memory_routes.register(app)


app_projects.register(app)


app_governed.register(app)


_PUBLIC_OPENAPI_SCHEMA: dict[str, Any] | None = None
_ADMIN_OPENAPI_SCHEMA: dict[str, Any] | None = None


def _public_openapi_schema() -> dict[str, Any]:
    global _PUBLIC_OPENAPI_SCHEMA
    if _PUBLIC_OPENAPI_SCHEMA is None:
        _PUBLIC_OPENAPI_SCHEMA = build_openapi_schema(app, "public")
    return _PUBLIC_OPENAPI_SCHEMA


def _admin_openapi_schema() -> dict[str, Any]:
    global _ADMIN_OPENAPI_SCHEMA
    if _ADMIN_OPENAPI_SCHEMA is None:
        _ADMIN_OPENAPI_SCHEMA = build_openapi_schema(app, "admin")
    return _ADMIN_OPENAPI_SCHEMA


# FastAPI's built-in /openapi.json handler calls app.openapi() lazily, so
# replacing the method after all routes are registered keeps the normal docs
# endpoint bounded to the public contract without moving route declarations.
app.openapi = _public_openapi_schema


@app.get("/openapi-public.json", include_in_schema=False)
def openapi_public() -> JSONResponse:
    return JSONResponse(_public_openapi_schema())


@app.get("/openapi-admin.json", include_in_schema=False)
def openapi_admin() -> JSONResponse:
    return JSONResponse(_admin_openapi_schema())


code_tools_service.configure(
    _SEMANTIC_READINESS_GATE_ENABLED=lambda: app_core_hook._SEMANTIC_READINESS_GATE_ENABLED,
    _fast_semantic_runtime_slo_status=lambda *a, **k: app_core_hook._fast_semantic_runtime_slo_status(*a, **k),
    _observation_store=lambda *a, **k: app_core_hook._observation_store(*a, **k),
    _read_git_head=lambda *a, **k: app_core_hook._read_git_head(*a, **k),
    _semantic_readiness_receipt=lambda *a, **k: app_core._semantic_readiness_receipt(*a, **k),
    bhm_health_slo=lambda *a, **k: app_telemetry.bhm_health_slo(*a, **k),
    federated_search=lambda *a, **k: app_core_fallback.federated_search(*a, **k),
)


llm_routes.configure(
    _authorize_resource_project=lambda *a, **k: app_core_hook._authorize_resource_project(*a, **k),
    _detect_conflicts=lambda *a, **k: app_ops._detect_conflicts(*a, **k),
    _detect_duplicates=lambda *a, **k: app_core._detect_duplicates(*a, **k),
    _llm_governor=lambda *a, **k: app_core_hook._llm_governor(*a, **k),
    _memory_matches_filters=lambda *a, **k: app_core_hit._memory_matches_filters(*a, **k),
    _relation_suggest=lambda *a, **k: app_ops._relation_suggest(*a, **k),
    federated_search=lambda *a, **k: app_core_fallback.federated_search(*a, **k),
)


galaxy_routes.configure(
    _memory_lifecycle=lambda *a, **k: app_core._memory_lifecycle(*a, **k),
    _memory_matches_filters=lambda *a, **k: app_core_hit._memory_matches_filters(*a, **k),
    _require_typed_memory_boundary=lambda *a, **k: app_core_fallback._require_typed_memory_boundary(*a, **k),
)


ui_routes.configure(
    _read_boot_report=lambda *a, **k: app_core_hook._read_boot_report(*a, **k),
)


memory_routes.configure(
    _RETRIEVAL_FUNNEL=lambda: app_core_hook._RETRIEVAL_FUNNEL,
    _admit_new_content=lambda *a, **k: app_core._admit_new_content(*a, **k),
    _advanced_search_live_memories=lambda *a, **k: app_retrieval._advanced_search_live_memories(*a, **k),
    _append_memory_changelog=lambda *a, **k: app_core._append_memory_changelog(*a, **k),
    _artifact_store_pairs=lambda *a, **k: app_ops_task2._artifact_store_pairs(*a, **k),
    _batch_mapping_items=lambda *a, **k: app_ops_admin._batch_mapping_items(*a, **k),
    _batch_project_scope=lambda *a, **k: app_ops_admin._batch_project_scope(*a, **k),
    _build_memory_title=lambda *a, **k: app_core._build_memory_title(*a, **k),
    _combined_review_status=lambda *a, **k: app_ops._combined_review_status(*a, **k),
    _create_memory_link=lambda *a, **k: app_ops._create_memory_link(*a, **k),
    _delete_live_memory=lambda *a, **k: app_ops_task2._delete_live_memory(*a, **k),
    _delete_live_memory_hard=lambda *a, **k: app_ops_task2._delete_live_memory_hard(*a, **k),
    _detect_conflicts=lambda *a, **k: app_ops._detect_conflicts(*a, **k),
    _detect_duplicates=lambda *a, **k: app_core._detect_duplicates(*a, **k),
    _fallback_grace_memories_response=lambda *a, **k: app_retrieval._fallback_grace_memories_response(*a, **k),
    _fetch_qdrant_hit_by_source_id=lambda *a, **k: app_core_hit._fetch_qdrant_hit_by_source_id(*a, **k),
    _is_archived_memory=lambda *a, **k: app_core._is_archived_memory(*a, **k),
    _is_fallback_grace_error=lambda *a, **k: app_retrieval._is_fallback_grace_error(*a, **k),
    _lint_memory_record=lambda *a, **k: app_ops._lint_memory_record(*a, **k),
    _memory_content_sha256=lambda *a, **k: app_ops._memory_content_sha256(*a, **k),
    _memory_matches_filters=lambda *a, **k: app_core_hit._memory_matches_filters(*a, **k),
    _memory_redact=lambda *a, **k: app_ops._memory_redact(*a, **k),
    _memory_review_queue=lambda *a, **k: app_ops._memory_review_queue(*a, **k),
    _merge_memories=lambda *a, **k: app_lifecycle._merge_memories(*a, **k),
    _normalize_linked_dependencies=lambda *a, **k: app_hooks._normalize_linked_dependencies(*a, **k),
    _normalize_memory_metadata=lambda *a, **k: app_ops_admin._normalize_memory_metadata(*a, **k),
    _project_matches=lambda *a, **k: app_core_hook._project_matches(*a, **k),
    _record_age_days=lambda *a, **k: app_projects._record_age_days(*a, **k),
    _relation_suggest=lambda *a, **k: app_ops._relation_suggest(*a, **k),
    _replace_live_memory=lambda *a, **k: app_core._replace_live_memory(*a, **k),
    _request_actor=lambda *a, **k: app_core_hook._request_actor(*a, **k),
    _request_ingress=lambda *a, **k: app_ops_task._request_ingress(*a, **k),
    _require_temporal_memory_boundary=lambda *a, **k: app_core_fallback._require_temporal_memory_boundary(*a, **k),
    _require_typed_memory_boundary=lambda *a, **k: app_core_fallback._require_typed_memory_boundary(*a, **k),
    _restore_archived_memory=lambda *a, **k: app_ops_admin._restore_archived_memory(*a, **k),
    _review_queue_id=lambda *a, **k: app_ops._review_queue_id(*a, **k),
    _save_live_memories=lambda *a, **k: app_core._save_live_memories(*a, **k),
    _search_hit_collection=lambda *a, **k: app_core_hit._search_hit_collection(*a, **k),
    _semantic_graph_node_id=lambda *a, **k: app_governed._semantic_graph_node_id(*a, **k),
    _serialize_conflict_candidate=lambda *a, **k: app_ops._serialize_conflict_candidate(*a, **k),
    _serialize_memory_link=lambda *a, **k: app_ops._serialize_memory_link(*a, **k),
    _serialize_memory_record=lambda *a, **k: app_core._serialize_memory_record(*a, **k),
    _set_memory_pin=lambda *a, **k: app_projects._set_memory_pin(*a, **k),
    _strict_retrieval_hits=lambda *a, **k: app_core_fallback._strict_retrieval_hits(*a, **k),
    _sync_mem0_record=lambda *a, **k: app_core._sync_mem0_record(*a, **k),
    _update_live_memory=lambda *a, **k: app_core._update_live_memory(*a, **k),
    _upsert_live_memory=lambda *a, **k: app_core._upsert_live_memory(*a, **k),
    _user_memory_metadata=lambda *a, **k: app_core._user_memory_metadata(*a, **k),
    federated_search=lambda *a, **k: app_core_fallback.federated_search(*a, **k),
)
