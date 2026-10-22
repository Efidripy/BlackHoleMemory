from __future__ import annotations

import time
import asyncio
import ctypes
import importlib.util
import json
import logging
import math
import os
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.routing import Route

from .resource_limits import BHM_FEDERATED_EMBEDDING_PREPARATION_TIMEOUT_SECONDS  # noqa: F401
from .resource_limits import BHM_FEDERATED_RETRIEVAL_CONTOUR_TIMEOUT_SECONDS  # noqa: F401
from .governed_consolidation import GovernedConsolidationRepository  # noqa: F401
from .governed_semantic_editor import GovernedSemanticEditorUnavailable  # noqa: F401
from .sync_service import InvalidTombstone  # noqa: F401
from .resource_limits import LLM_HTTP_TIMEOUT_SECONDS  # noqa: F401
from .governed_semantic_editor import LocalGatewaySemanticCompletion  # noqa: F401
from .llm_gateway import LocalOpenAICompatibleAdapter  # noqa: F401
from .context_compiler import MAX_CONTEXT_ITEM_CHARS  # noqa: F401
from .observation_security import OBSERVATION_COMPACT_MAX_INPUT_BYTES  # noqa: F401
from .observation_security import OBSERVATION_IDLE_MAX_INPUT_BYTES  # noqa: F401
from .observation_security import OBSERVATION_MAX_INPUT_BYTES  # noqa: F401
from .resource_limits import PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS  # noqa: F401
from .sync_service import UndoWindowExpired  # noqa: F401
from .app_runtime import _run_bounded_read  # noqa: F401
from .shared_memory_audit import append_shared_memory_audit  # noqa: F401
from .utility_feedback_store import append_utility_event  # noqa: F401
from .change_impact import build_change_impact_preview  # noqa: F401
from .graph import build_graph  # noqa: F401
from .mcp_repair import build_repair_preview  # noqa: F401
from .mcp_repair import build_reprobe  # noqa: F401
from .governed_semantic_editor import build_semantic_proposal  # noqa: F401
from .change_impact import collect_git_change_paths  # noqa: F401
from .change_impact import collect_git_history_stats  # noqa: F401
from .context_tier_promotion import runtime_enabled as context_tier_promotion_enabled  # noqa: F401
import copy  # noqa: F401
from .health import dependency_report  # noqa: F401
from .exact_identifier_retrieval import exact_identifier_enabled  # noqa: F401
from .mem0_adapter import get_global_core_memory  # noqa: F401
from .mem0_adapter import get_project_mem0_memory  # noqa: F401
from .governed_consolidation import runtime_enabled as governed_consolidation_enabled  # noqa: F401
from .governed_consolidation import runtime_status as governed_consolidation_status  # noqa: F401
from .mem0_adapter import lexical_score  # noqa: F401
from .memory_doctor import load_authoritative_sqlite_snapshot  # noqa: F401
from .utility_feedback_store import load_utility_events  # noqa: F401
from .mem0_adapter import local_collection_name  # noqa: F401
from .mem0_adapter import mem0_runtime_plan  # noqa: F401
from .projection_generation_binding import resolve_projection_route  # noqa: F401
from .context_tier_promotion import rollback_tier_promotion  # noqa: F401
from .memory_doctor import run_authoritative_sqlite_memory_doctor  # noqa: F401
import sqlite3  # noqa: F401
from .temporal_contract import temporal_contract_enabled  # noqa: F401
from datetime import timedelta  # noqa: F401
from .typed_memory_contract import typed_memory_capability_available  # noqa: F401
from . import app_core
from . import app_governed
from . import app_graphs
from . import app_hooks
from . import app_mcp
from . import app_ops
from . import app_projects
from . import app_retrieval
from . import app_telemetry
from . import memory_routes
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
from .ui_session import BOOTSTRAP_TTL_SECONDS  # noqa: F401
from .ui_session import SESSION_TTL_SECONDS  # noqa: F401
from .ui_session import UiSessionRegistry  # noqa: F401
from . import ui_routes
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
from .resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS  # noqa: F401
from . import galaxy_routes
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
from .mcp_surfaces import CORE_TOOL_NAMES  # noqa: F401
from .repository_intelligence import collect_repository_files  # noqa: F401
from . import llm_routes
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
from .resource_limits import BHM_CODE_COVERAGE_PROBE_TIMEOUT_SECONDS  # noqa: F401
from .resource_limits import BHM_CODE_GRAPH_SOFT_WAIT_SECONDS  # noqa: F401
from .resource_limits import BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS  # noqa: F401
from .resource_limits import BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS  # noqa: F401
from .resource_limits import BHM_INDEX_MAX_FILES_PER_RUN  # noqa: F401
from .caller_auth import authorize_project_root  # noqa: F401
from .architecture_intelligence import build_architecture_explain_receipt  # noqa: F401
from .architecture_intelligence import build_architecture_intelligence  # noqa: F401
from .architecture_intelligence import build_architecture_memory  # noqa: F401
from .architecture_intelligence import build_graph_analysis_quality_receipt  # noqa: F401
from . import graph_build_jobs  # noqa: F401
from .repository_index import index_repository  # noqa: F401
from .repository_index import repository_index_status  # noqa: F401
from . import code_tools_service
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
from .memory_pulse_bus import MemoryPulseBus  # noqa: F401
import urllib.request  # noqa: F401
from . import app_runtime as rt
from .app_runtime import (
    ResponseTimeout,
    _MAX_CONCURRENT_WRITES,
    _env_float,
    _safe_exception_text,
    _write_json_atomic,
)  # noqa: F401
from .api_models import (
    FactCrystalDomain,  # noqa: F401
    FactCrystalPriority,  # noqa: F401
    FactCrystalSemanticType,  # noqa: F401
    UiSessionExchangeRequest,  # noqa: F401
    SearchRequest,  # noqa: F401
    RememberRequest,  # noqa: F401
    MemoryUpdateRequest,  # noqa: F401
    MemoryArchiveRequest,  # noqa: F401
    ForgetPreviewRequest,  # noqa: F401
    ForgetApplyRequest,  # noqa: F401
    MemoryAdvancedSearchRequest,  # noqa: F401
    ContextCompileRequest,  # noqa: F401
    AgentContextRequest,  # noqa: F401
    ContextTierPromotionRollbackRequest,  # noqa: F401
    RetrievalExplainRequest,  # noqa: F401
    MemoryUsedRequest,  # noqa: F401
    UtilityFeedbackEventRequest,  # noqa: F401
    ConsolidationChangeSetPreviewRequest,  # noqa: F401
    ConsolidationChangeSetReviewRequest,  # noqa: F401
    GovernedConsolidationCreateRequest,  # noqa: F401
    GovernedSemanticProposalRequest,  # noqa: F401
    GovernedConsolidationDecisionRequest,  # noqa: F401
    GovernedConsolidationProposalRequest,  # noqa: F401
    GovernedConsolidationApplyRequest,  # noqa: F401
    McpRepairRequest,  # noqa: F401
    MemoryRecentActivityRequest,  # noqa: F401
    GalaxyDataNode,  # noqa: F401
    GalaxyDataLink,  # noqa: F401
    GalaxyDataResponse,  # noqa: F401
    GalaxyStatsResponse,  # noqa: F401
    LauncherStatsResponse,  # noqa: F401
    MemoryUpsertRequest,  # noqa: F401
    BatchMemoryUpsertItem,  # noqa: F401
    MemoryLinkRequest,  # noqa: F401
    BatchMemoryLinkItem,  # noqa: F401
    MemoryLinkDeleteRequest,  # noqa: F401
    SharedMemoryPolicyPreflightRequest,  # noqa: F401
    SharedMemoryReadRequest,  # noqa: F401
    MemoryCrystallizeRequest,  # noqa: F401
    FactSynthesisThreeZoneContext,  # noqa: F401
    FactSynthesisRequest,  # noqa: F401
    LLMJobSubmitRequest,  # noqa: F401
    LLMCandidatePlanRequest,  # noqa: F401
    LLMDelegationDecisionRequest,  # noqa: F401
    MemoryFoundryPreviewRequest,  # noqa: F401
    RetrievalLabPreviewRequest,  # noqa: F401
    RepositoryIntelligencePreviewRequest,  # noqa: F401
    CodeGraphQueryRequest,  # noqa: F401
    PublicCodeToolRequest,  # noqa: F401
    ConventionMemoryPreviewRequest,  # noqa: F401
    ChangeImpactPreviewRequest,  # noqa: F401
    UnifiedContextCompileRequest,  # noqa: F401
    SessionCapturePreviewRequest,  # noqa: F401
    MemoryGraphQueryRequest,  # noqa: F401
    TaskGraphQueryRequest,  # noqa: F401
    LLMCodeFabricPlanRequest,  # noqa: F401
    FactoryIntegrationPreviewRequest,  # noqa: F401
    UnifiedMcpContractPreviewRequest,  # noqa: F401
    CapabilityRoutePreviewRequest,  # noqa: F401
    HumanUiBridgePreviewRequest,  # noqa: F401
    MigrationPreviewRequest,  # noqa: F401
    SecurityTrustBoundaryPreviewRequest,  # noqa: F401
    QAIncidentPreviewRequest,  # noqa: F401
    DocumentationFactoryPreviewRequest,  # noqa: F401
    NightShiftPreviewRequest,  # noqa: F401
    ModelRouterDecisionRequest,  # noqa: F401
    LLMCachePreviewRequest,  # noqa: F401
    LLMLearningReviewRequest,  # noqa: F401
    LLMLearningCurateRequest,  # noqa: F401
    FactCrystal,  # noqa: F401
    CheckpointCreateRequest,  # noqa: F401
    ProjectMapUpsertRequest,  # noqa: F401
    MemoryMergeRequest,  # noqa: F401
    MemoryDetectRequest,  # noqa: F401
    MemoryLintRequest,  # noqa: F401
    MemoryDeleteRequest,  # noqa: F401
    MemoryConfidenceRequest,  # noqa: F401
    MemoryPinRequest,  # noqa: F401
    MemoryVoteRequest,  # noqa: F401
    MemorySourceRefsRequest,  # noqa: F401
    AdrCreateRequest,  # noqa: F401
    AdrSupersedeRequest,  # noqa: F401
    HandoffCreateRequest,  # noqa: F401
    SessionRecordCreateRequest,  # noqa: F401
    TaskOpenRequest,  # noqa: F401
    TaskCloseRequest,  # noqa: F401
    TaskIngressControlRequest,  # noqa: F401
    TaskContextUpdateRequest,  # noqa: F401
    RiskRegisterUpdateRequest,  # noqa: F401
    ValidationSnapshotSaveRequest,  # noqa: F401
    MemoryTimelineRequest,  # noqa: F401
    HardDeleteMemoryRequest,  # noqa: F401
    BatchUpsertMemoriesRequest,  # noqa: F401
    BatchLinkMemoriesRequest,  # noqa: F401
    BatchAttachSourceRefsRequest,  # noqa: F401
    SourceRefsReplaceRequest,  # noqa: F401
    SourceRefsDetachRequest,  # noqa: F401
    RestoreMemoryRequest,  # noqa: F401
    BatchMemoryIdsRequest,  # noqa: F401
    RepairLiveIndexesRequest,  # noqa: F401
    RebuildProjectSummaryRequest,  # noqa: F401
    ProjectSummaryListRequest,  # noqa: F401
    EntityExtractRequest,  # noqa: F401
    RelationSuggestRequest,  # noqa: F401
    MemoryCompactRequest,  # noqa: F401
    PolicyGuardRequest,  # noqa: F401
    MemoryDiffRequest,  # noqa: F401
    SearchByRefRequest,  # noqa: F401
    SearchByUpsertKeyRequest,  # noqa: F401
    BatchRestoreRequest,  # noqa: F401
    ArtifactRestoreRequest,  # noqa: F401
    OrphanArtifactRelinkRequest,  # noqa: F401
    MemoryStalenessReportRequest,  # noqa: F401
    MemoryReviewQueueRequest,  # noqa: F401
    MemoryTriageQueueRequest,  # noqa: F401
    ProjectSummaryRefreshAllRequest,  # noqa: F401
    RelationApplySuggestionsRequest,  # noqa: F401
    MemoryMergePreviewRequest,  # noqa: F401
    SchemaUpgradeAllRequest,  # noqa: F401
    MemoryRedactRequest,  # noqa: F401
    SecretScanRequest,  # noqa: F401
    RelationConfidenceRequest,  # noqa: F401
    RelationVoteRequest,  # noqa: F401
    MemoryAliasRequest,  # noqa: F401
    AliasResolveRequest,  # noqa: F401
    EntityCatalogRequest,  # noqa: F401
    ProjectSummaryCompareRequest,  # noqa: F401
    RecentFailuresFeedRequest,  # noqa: F401
    ProjectOnlyRequest,  # noqa: F401
    ScopedMaintenanceRequest,  # noqa: F401
    ProjectRetirementRequest,  # noqa: F401
    ProjectSummaryPinRequest,  # noqa: F401
    MemorySchemaValidateRequest,  # noqa: F401
    ArtifactDeleteRequest,  # noqa: F401
    ArtifactListRequest,  # noqa: F401
    MemoryGcCandidatesRequest,  # noqa: F401
    MemoryCompactionReportRequest,  # noqa: F401
    LinkCycleDetectRequest,  # noqa: F401
    ProjectMapCompareRequest,  # noqa: F401
    ValidationTrendReportRequest,  # noqa: F401
    EntitySearchRequest,  # noqa: F401
    EntityLinkMemoriesRequest,  # noqa: F401
    RelationPruneLowQualityRequest,  # noqa: F401
    ProjectSimilarityReportRequest,  # noqa: F401
    MemoryChangelogRequest,  # noqa: F401
    ReviewQueueApplyRequest,  # noqa: F401
    TriageQueueApplyRequest,  # noqa: F401
    HardDeleteRestorePreviewRequest,  # noqa: F401
    ArtifactBatchDeleteRequest,  # noqa: F401
    ArtifactBatchRelinkRequest,  # noqa: F401
    ArtifactBatchRestoreRequest,  # noqa: F401
    StrictSchemaValidateRequest,  # noqa: F401
    IntegrityRepairStrictRequest,  # noqa: F401
    AdminExportRequest,  # noqa: F401
    AdminImportPreviewRequest,  # noqa: F401
    AdminImportApplyRequest,  # noqa: F401
    PolicyProfileSetRequest,  # noqa: F401
    PolicyEnforceMemoryRequest,  # noqa: F401
    OverlapReportRequest,  # noqa: F401
    OverlapCleanupApplyRequest,  # noqa: F401
    TypeMigrateRequest,  # noqa: F401
    HybridSearchRequest,  # noqa: F401
    BhmMatchSearchRequest,  # noqa: F401
    ReflectRequest,  # noqa: F401
    SlotRequest,  # noqa: F401
    SlotAppendRequest,  # noqa: F401
    SlotReplaceRequest,  # noqa: F401
    SlotLabelRequest,  # noqa: F401
    LessonRequest,  # noqa: F401
    LessonStrengthenRequest,  # noqa: F401
    MemoryVerifyRequest,  # noqa: F401
    BhmHookRequest,  # noqa: F401
    BhmHookCompactRequest,  # noqa: F401
    BhmHookIdleRequest,  # noqa: F401
)  # noqa: F401
from .config import settings
from .local_endpoint_policy import open_local_url
from .resource_limits import PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS
from .resource_limits import PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS
from .resource_limits import PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS
from .resource_limits import QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS
from .capability import ADMIN_CAPABILITY_HEADER
from .capability import admin_route_requires_capability
from .capability import is_admin_capability_valid
from .caller_auth import authorize_projects
from .caller_auth import caller_auth_configuration_error
from .caller_auth import caller_route_policy
from .caller_auth import caller_project_scope_requires_explicit
from .caller_auth import CallerRoutePolicy
from .caller_auth import configured_caller_principal
from .caller_auth import extract_request_projects
from .caller_auth import is_caller_token_valid
from .caller_auth import MAX_PROJECT_INSPECTION_BYTES
from .caller_auth import parse_bearer_token
from .ui_session import UI_SESSION_COOKIE
from .ui_session import ui_session_route_allowed
from .storage_state import qdrant_required_for_core
from .mem0_adapter import StorageNotReady
from .mem0_adapter import ensure_memory_collections
from .mem0_adapter import ensure_decay_metadata
from .mem0_adapter import get_qdrant_client
from .mem0_adapter import global_collection_name
from .mem0_adapter import memory_decay_score
from .mem0_adapter import storage_runtime_state
from .mem0_adapter import _remote_qdrant_available
from .mcp_surfaces import resolve_mcp_surface
from .openapi_contract import build_openapi_schema
from .runtime_endpoints import validate_loopback_listener_host
from .context_tier_lifecycle import build_context_tier_lifecycle_receipt
from .runtime_storage import MemoryStoreMode
from .runtime_storage import resolve_runtime_storage_config
from .hook_queue import HookJobLeaseLost
from .hook_queue import HookJobQueue
from .hook_queue import HookQueueError
from .observation_contract import ObservationIngressV1
from .observation_contract import build_observation_record
from .sqlite_retention import automatic_sqlite_retention_loop
from .sqlite_retention import SQLiteRetentionPolicy
from .observation_security import observation_body_limit
from .observation_security import redact_secret_text
from .usage_telemetry import monotonic_elapsed_ms
from .usage_telemetry import normalize_operation
from .surface_report import build_surface_report


_LOGGER = logging.getLogger(__name__)


_HOOK_QUEUE_POLL_SECONDS = _env_float("BHM_HOOK_QUEUE_POLL_SECONDS", 0.05, 0.01)
_HOOK_QUEUE_RETRY_BASE_SECONDS = _env_float("BHM_HOOK_QUEUE_RETRY_BASE_SECONDS", 1.0, 0.0)
_HOOK_QUEUE_BOOT_ID = f"bhm-hook-worker-{uuid.uuid4().hex[:12]}"
_STORAGE_STARTUP_TIMEOUT_SECONDS = _env_float(
    "BHM_STORAGE_STARTUP_TIMEOUT_SECONDS",
    30.0,
    0.1,
    PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS,
)
_QDRANT_HEALTH_TIMEOUT_SECONDS = _env_float(
    "BHM_QDRANT_HEALTH_TIMEOUT_SECONDS",
    QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS,
    0.1,
    QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS,
)
_KNOWLEDGE_COUNTERS_SCHEMA_VERSION = "bhm.dashboard.knowledge-counters.v1"
_DASHBOARD_STATUS_SCHEMA_VERSION = "bhm.dashboard.status.v1"
_ARCHITECTURE_MEMORY_TYPES = frozenset({"architecture", "adr"})
_ARCHITECTURE_TAXONOMY_TAGS = frozenset({"architecture", "law", "adr"})
_BOOT_REPORT_QDRANT_POLL_SECONDS = 0.25


if not rt._PROVIDER_WARMUP_REQUIRED:
    rt._PROVIDER_WARMUP_READY.set()


def _hook_job_result_summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"success": True, "resultType": type(result).__name__}
    summary: dict[str, Any] = {
        "success": bool(result.get("success", True)),
        "action": str(result.get("action") or "completed")[:200],
    }
    if result.get("reason"):
        summary["reason"] = redact_secret_text(str(result["reason"])).value[:1000]
    observation = result.get("observation")
    if isinstance(observation, dict):
        summary["observation"] = {"id": str(observation.get("id") or observation.get("eventId") or "")}
    memory = result.get("memory")
    if isinstance(memory, dict):
        summary["memory"] = {"id": str(memory.get("id") or memory.get("source_id") or "")}
    if isinstance(result.get("source_ids"), list):
        summary["sourceCount"] = len(result["source_ids"])
    tier_lifecycle = result.get("tier_lifecycle")
    if isinstance(tier_lifecycle, dict):
        summary["tierLifecycle"] = {
            "receiptId": str(tier_lifecycle.get("receipt_id") or ""),
            "phase": str(tier_lifecycle.get("phase") or "unmapped"),
            "effectClass": str(tier_lifecycle.get("effect_class") or "none"),
            "promotion": str((tier_lifecycle.get("promotion") or {}).get("action") or "none"),
        }
    steps = result.get("steps")
    if isinstance(steps, dict):
        summary["steps"] = {
            str(name): {
                "success": bool(value.get("success", True)) if isinstance(value, dict) else True,
                "status": str(value.get("status") or "completed") if isinstance(value, dict) else "completed",
            }
            for name, value in steps.items()
        }
    return summary


def _hook_job_log_context(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return {
        "job_id": str(job.get("jobId") or ""),
        "event_id": str(job.get("eventId") or payload.get("eventId") or ""),
        "correlation_id": str(payload.get("correlationId") or payload.get("sessionId") or ""),
        "kind": str(job.get("kind") or ""),
        "attempts": int(job.get("attempts") or 0),
        "max_attempts": int(job.get("maxAttempts") or 0),
    }


async def _execute_hook_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise HookQueueError("hook job payload missing")
    kind = str(job.get("kind") or "")
    if kind == "compact":
        request = BhmHookCompactRequest.model_validate(payload)
        return await asyncio.to_thread(_handle_compact_hook, request)
    if kind == "idle":
        request = BhmHookIdleRequest.model_validate(payload)
        return await _run_idle_reflection_pipeline(request)
    raise HookQueueError(f"unsupported hook job kind: {kind}")


async def _hook_job_heartbeat(queue: HookJobQueue, job_id: str, owner: str) -> None:
    interval = max(app_core._HOOK_QUEUE_LEASE_SECONDS / 3.0, 1.0)
    while True:
        await asyncio.sleep(interval)
        renewed = await asyncio.to_thread(
            queue.renew_lease,
            job_id,
            owner=owner,
            lease_seconds=app_core._HOOK_QUEUE_LEASE_SECONDS,
        )
        if not renewed:
            return


async def _hook_queue_worker(*, worker_name: str, kinds: tuple[str, ...], stop_event: asyncio.Event) -> None:
    queue = app_core._hook_queue()
    owner = f"{_HOOK_QUEUE_BOOT_ID}:{worker_name}"
    while True:
        try:
            job = await asyncio.to_thread(
                queue.claim_next,
                kinds=kinds,
                owner=owner,
                lease_seconds=app_core._HOOK_QUEUE_LEASE_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[WARN] Hook queue claim failed for {worker_name}: {_safe_exception_text(exc)}", flush=True)
            if stop_event.is_set():
                return
            await asyncio.sleep(_HOOK_QUEUE_POLL_SECONDS)
            continue
        if job is None:
            if stop_event.is_set():
                return
            await asyncio.sleep(_HOOK_QUEUE_POLL_SECONDS)
            continue

        job_id = str(job["jobId"])
        heartbeat = asyncio.create_task(_hook_job_heartbeat(queue, job_id, owner))
        try:
            result = await _execute_hook_job(job)
            await asyncio.to_thread(
                queue.complete,
                job_id,
                owner=owner,
                result=_hook_job_result_summary(result),
            )
        except asyncio.CancelledError:
            _LOGGER.info(
                "hook_job_cancelled",
                extra={**_hook_job_log_context(job), "classification": "cancelled"},
            )
            raise
        except Exception as exc:
            redacted_error = _safe_exception_text(exc, limit=4000)
            retry_delay = _HOOK_QUEUE_RETRY_BASE_SECONDS * (2 ** max(int(job.get("attempts") or 1) - 1, 0))
            try:
                status = await asyncio.to_thread(
                    queue.fail,
                    job_id,
                    owner=owner,
                    error=redacted_error,
                    retry_delay_seconds=retry_delay,
                )
                _LOGGER.warning(
                    "hook_job_failed",
                    extra={
                        **_hook_job_log_context(job),
                        "classification": "retryable" if status == "queued" else "terminal",
                        "status": status,
                        "error": redacted_error,
                    },
                )
            except HookJobLeaseLost:
                _LOGGER.error(
                    "hook_job_failure_recording_lease_lost",
                    extra={**_hook_job_log_context(job), "classification": "lease_lost"},
                )
            except Exception as record_exc:
                _LOGGER.exception(
                    "hook_job_failure_recording_failed",
                    extra={
                        **_hook_job_log_context(job),
                        "classification": "failure_recording",
                        "error": redact_secret_text(str(record_exc)).value[:4000],
                    },
                )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass


async def _start_hook_queue_workers() -> None:
    pass  # globals moved to domain modules
    if any(not task.done() for task in app_hooks._HOOK_QUEUE_TASKS):
        app_hooks._HOOK_QUEUE_ACCEPTING = True
        return
    queue = app_core._hook_queue()
    recovered = await asyncio.to_thread(queue.recover_processing)
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    for app_ops.index in range(app_core._HOOK_COMPACT_WORKERS):
        tasks.append(
            asyncio.create_task(
                _hook_queue_worker(
                    worker_name=f"compact-{app_ops.index + 1}",
                    kinds=("compact",),
                    stop_event=stop_event,
                ),
                name=f"bhm-hook-compact-{app_ops.index + 1}",
            )
        )
    for app_ops.index in range(app_core._HOOK_IDLE_WORKERS):
        tasks.append(
            asyncio.create_task(
                _hook_queue_worker(
                    worker_name=f"idle-{app_ops.index + 1}",
                    kinds=("idle",),
                    stop_event=stop_event,
                ),
                name=f"bhm-hook-idle-{app_ops.index + 1}",
            )
        )
    app_core._HOOK_QUEUE_STOP_EVENT = stop_event
    app_hooks._HOOK_QUEUE_TASKS = tasks
    app_hooks._HOOK_QUEUE_ACCEPTING = True
    print(
        f"[INFO] BHM hook queue ready: capacity={queue.capacity} "
        f"compact_workers={app_core._HOOK_COMPACT_WORKERS} idle_workers={app_core._HOOK_IDLE_WORKERS} recovered={recovered}",
        flush=True,
    )


async def _stop_hook_queue_workers() -> None:
    pass  # globals moved to domain modules
    app_hooks._HOOK_QUEUE_ACCEPTING = False
    stop_event = app_core._HOOK_QUEUE_STOP_EVENT
    tasks = list(app_hooks._HOOK_QUEUE_TASKS)
    if stop_event is not None:
        stop_event.set()
    if tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=app_core._HOOK_QUEUE_DRAIN_SECONDS,
            )
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            print(
                f"[WARN] Hook queue drain exceeded {app_core._HOOK_QUEUE_DRAIN_SECONDS:.1f}s; "
                "leased jobs will be recovered on restart.",
                flush=True,
            )
    app_hooks._HOOK_QUEUE_TASKS = []
    app_core._HOOK_QUEUE_STOP_EVENT = None


def _optional_psutil():
    try:
        import psutil

        return psutil
    except ImportError:
        return None


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _current_working_set_mb() -> float:
    psutil = _optional_psutil()
    if psutil is not None:
        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
    if os.name == "nt":
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if ok:
            return round(counters.WorkingSetSize / (1024 * 1024), 1)
    return 0.0


def _wsl_shared_overhead_gb() -> float:
    psutil = _optional_psutil()
    if psutil is None:
        return 0.0
    total = 0
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            name = str(proc.info.get("name") or "").lower()
            if name in {"vmmemwsl.exe", "vmmemwsl", "vmmem.exe", "vmmem"}:
                total += int(proc.info["memory_info"].rss)
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue
    return round(total / (1024 * 1024 * 1024), 2)


def _qdrant_healthy_sync() -> bool:
    return _remote_qdrant_available(opener=open_local_url, timeout=_QDRANT_HEALTH_TIMEOUT_SECONDS)


async def _wait_for_required_storage_ready(timeout_seconds: float | None = None):
    timeout = _STORAGE_STARTUP_TIMEOUT_SECONDS if timeout_seconds is None else max(float(timeout_seconds), 0.0)
    deadline = time.monotonic() + timeout
    while True:
        state = await asyncio.to_thread(storage_runtime_state)
        if state.ready or state.configured_mode != "remote-required":
            return state
        if time.monotonic() >= deadline:
            raise StorageNotReady(
                f"required storage did not become ready within {timeout:.1f}s: {state.reason}"
            )
        await asyncio.sleep(_BOOT_REPORT_QDRANT_POLL_SECONDS)


async def _wait_for_qdrant_ready() -> None:
    await _wait_for_required_storage_ready()


def _is_architecture_decision(record: Mapping[str, Any]) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    memory_type = str(record.get("memory_type") or metadata.get("memory_type") or "").strip().lower()
    if memory_type in _ARCHITECTURE_MEMORY_TYPES:
        return True
    tags = record.get("tags") or metadata.get("tags") or []
    concepts = {str(item).strip().lower() for item in tags if str(item).strip()}
    semantic_type = str(metadata.get("semantic_type") or "").strip().lower()
    return semantic_type == "decision-log" and bool(_ARCHITECTURE_TAXONOMY_TAGS & concepts)


def _knowledge_counter_group(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"active_count": 0, "archived_count": 0, "tombstoned_count": 0}
    for record in records:
        counts[f"{app_core._memory_lifecycle(record)}_count"] += 1
    return {**counts, "total_count": sum(counts.values())}


def _knowledge_counters_sync(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    source_records = records if records is not None else rt._load_live_memories()
    architecture_records = [record for record in source_records if _is_architecture_decision(record)]
    projects = sorted({str(record.get("project") or "") for record in source_records if record.get("project")})
    return {
        "schema_version": _KNOWLEDGE_COUNTERS_SCHEMA_VERSION,
        "source": {
            "authority": "sqlite",
            "table": "memories",
            "projection_used": False,
        },
        "scope": {
            "kind": "all-local-projects",
            "project_filter": None,
            "project_count": len(projects),
        },
        "memory_records": {
            "label": "Active memory records",
            **_knowledge_counter_group(source_records),
            "definition": "All SQLite memory records grouped by lifecycle.",
        },
        "architecture_decisions": {
            "label": "Active architecture decisions",
            **_knowledge_counter_group(architecture_records),
            "definition": (
                "SQLite memory records classified as architecture or ADR by memory type, "
                "or as decision-log records with architecture taxonomy tags."
            ),
        },
    }


def _collect_host_telemetry_sync() -> dict[str, Any]:
    queue_status = (
        app_core._hook_queue().status()
        if app_core._hook_queue_path().exists()
        else {
            "pending": 0,
            "capacity": app_core._HOOK_QUEUE_CAPACITY,
            "counts": {"queued": 0, "processing": 0, "completed": 0, "failed": 0},
            "oldestQueuedAgeMs": 0,
        }
    )
    return {
        "bhm_working_set_mb": _current_working_set_mb(),
        "wsl_shared_overhead_gb": _wsl_shared_overhead_gb(),
        "qdrant_healthy": _qdrant_healthy_sync(),
        "knowledge_counters": _knowledge_counters_sync(),
        "hook_queue_pending": queue_status["pending"],
        "hook_queue_capacity": queue_status["capacity"],
        "hook_queue_queued": queue_status["counts"]["queued"],
        "hook_queue_processing": queue_status["counts"]["processing"],
        "hook_queue_failed": queue_status["counts"]["failed"],
        "hook_queue_oldest_age_ms": queue_status["oldestQueuedAgeMs"],
    }


async def _collect_sys_status_payload() -> dict[str, Any]:
    host = await asyncio.to_thread(_collect_host_telemetry_sync)
    warmup = rt._get_provider_warmup_status()
    return {
        "event": "sys_status",
        "data": {
            "schema_version": _DASHBOARD_STATUS_SCHEMA_VERSION,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mcp_active_pipes": 0,
            "mcp_max_instances": 0,
            "mcp_surface": resolve_mcp_surface().value,
            "mcp_max_frame_bytes": 0,
            "mcp_client_timeout_seconds": 0.0,
            "mcp_dispatch_timeout_seconds": 0.0,
            "mcp_transport": app_core._MCP_STREAMABLE_HTTP.contract_snapshot()["sessions"],
            "storage": storage_runtime_state().as_dict(),
            "memory_store": rt._memory_store_state().as_dict(),
            "fallback_mode": app_core._configured_fallback_mode(),
            "backpressure_semaphore_value": getattr(rt._WRITE_SEMAPHORE, "_value", 0),
            "backpressure_max": _MAX_CONCURRENT_WRITES,
            "fallback_grace_active": app_core._fallback_grace_active(),
            "launcher_circuit_breaker_status": "STREAMABLE_HTTP",
            "llm_warmup_status": "READY" if warmup.get("ready") else str(warmup.get("phase") or "WARMING").upper(),
            **host,
        },
    }


async def _telemetry_harvester_loop() -> None:
    while True:
        try:
            await asyncio.sleep(app_core._TELEMETRY_INTERVAL_SECONDS)
            if rt._MEMORY_PULSE_BUS.client_count == 0:
                continue
            await rt._MEMORY_PULSE_BUS.broadcast(await _collect_sys_status_payload())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[WARN] BHM telemetry harvester skipped tick: {_safe_exception_text(exc)}", flush=True)


MAX_UI_EXCHANGE_BODY_BYTES = 16 * 1024


async def _cancel_lifespan_task(task: asyncio.Task[None] | None, *, name: str) -> None:
    """Cancel and observe one lifespan task without aborting sibling cleanup.

    A background task can fail before shutdown starts.  Awaiting that failed
    task directly from the lifespan ``finally`` block would skip cancellation
    of later tasks and the infrastructure cleanup call.  Shutdown is a
    best-effort boundary: preserve the primary lifecycle exception while
    observing secondary task failures and continuing cleanup.
    """

    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        _LOGGER.exception("BHM lifespan task failed during shutdown: %s", name)


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
        await asyncio.to_thread(_initialize_authoritative_memory_service)
    storage = await asyncio.to_thread(storage_runtime_state)
    projection_required = qdrant_required_for_core() or not sqlite_authoritative
    if projection_required:
        waited_storage = await _wait_for_required_storage_ready()
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
        if _boot_report_is_pending():
            boot_report_task = asyncio.create_task(_finalize_pending_boot_report(warmup_task))
        telemetry_task = asyncio.create_task(_telemetry_harvester_loop())
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
        await _start_hook_queue_workers()
        async with app_core._MCP_STREAMABLE_HTTP.run():
            yield
    finally:
        _UI_SESSIONS.reset()
        try:
            await _stop_hook_queue_workers()
        finally:
            await _cancel_lifespan_task(boot_report_task, name="boot_report")
            await _cancel_lifespan_task(telemetry_task, name="telemetry")
            await _cancel_lifespan_task(sqlite_retention_task, name="sqlite_retention")
            await _cancel_lifespan_task(warmup_task, name="provider_warmup")
            await _cleanup_registered_infra_processes(reason="api_shutdown")
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
        if content_length > MAX_UI_EXCHANGE_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": {"code": "ui_bootstrap_payload_too_large"}},
            )
        raw_body = await request.body()
        if len(raw_body) > MAX_UI_EXCHANGE_BODY_BYTES:
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


def _rest_usage_operation(request: Request) -> str:
    route = request.scope.get("route")
    route_template = getattr(route, "path", None)
    return normalize_operation(
        f"{request.method.upper()} {route_template or request.url.path}",
        fallback=f"{request.method.upper()} other",
    )


def _rest_usage_status(status_code: int) -> str:
    if 200 <= status_code < 300:
        return "2xx"
    if 300 <= status_code < 400:
        return "3xx"
    if 400 <= status_code < 500:
        return "4xx"
    if 500 <= status_code < 600:
        return "5xx"
    return "error"


def _response_size_from_headers(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw_size = headers.get("content-length")
    if raw_size is None:
        return None
    try:
        return max(int(raw_size), 0)
    except (TypeError, ValueError):
        return None


@app.middleware("http")
async def usage_telemetry_guard(request: Request, call_next):
    started_at = time.perf_counter()
    response = None
    status = "exception"
    timed_out = False
    try:
        response = await call_next(request)
        status = _rest_usage_status(int(response.status_code))
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
            surface=app_core._request_surface(request),
            operation=_rest_usage_operation(request),
            status=status,
            duration_ms=monotonic_elapsed_ms(started_at),
            response_size_bytes=_response_size_from_headers(response),
            timeout=timed_out,
        )


def _initialize_authoritative_memory_service() -> None:
    rt._memory_service().repository.initialize()


def _boot_report_is_pending() -> bool:
    return app_core._read_boot_report().get("status") == "pending"


async def _finalize_pending_boot_report(warmup_task: asyncio.Task[None]) -> None:
    pending = app_core._read_boot_report()
    if pending.get("status") != "pending":
        return

    try:
        trigger_time = float(pending.get("trigger_time") or time.time())
    except (TypeError, ValueError):
        trigger_time = time.time()

    await warmup_task
    await _wait_for_qdrant_ready()
    _write_json_atomic(
        app_core._BOOT_REPORT_PATH,
        {
            "status": "SUCCESS",
            "elapsed_seconds": round(max(time.time() - trigger_time, 0.0), 2),
            "qdrant": "OK",
            "lm_studio": "READY",
            "timestamp": rt._utc_now_iso(),
        },
    )


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return f'"{pid}"' in result.stdout or f",{pid}," in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _bounded_process_termination_grace_seconds(value: Any) -> int:
    try:
        requested = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("termination grace must be numeric") from exc
    if not math.isfinite(requested):
        raise ValueError("termination grace must be finite")
    return max(min(int(requested), PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS), 1)


async def _terminate_process_tree(
    pid: int,
    *,
    grace_seconds: int = PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS,
    dry_run: bool = False,
) -> dict[str, Any]:
    grace_seconds = _bounded_process_termination_grace_seconds(grace_seconds)
    result: dict[str, Any] = {"pid": pid, "dry_run": dry_run, "terminated": False, "forced": False}
    if pid <= 0:
        result["error"] = "invalid_pid"
        return result
    if dry_run:
        result["running"] = _is_pid_running(pid)
        return result
    if os.name == "nt":
        try:
            soft = await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True,
                text=True,
                timeout=max(grace_seconds, 1),
                check=False,
            )
            result["soft_exit_code"] = soft.returncode
            result["soft_output"] = (soft.stdout or soft.stderr or "").strip()[:500]
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["soft_error"] = _safe_exception_text(exc)
        await asyncio.sleep(max(grace_seconds, 0))
        if _is_pid_running(pid):
            try:
                forced = await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=max(grace_seconds, 1),
                    check=False,
                )
                result["forced"] = True
                result["force_exit_code"] = forced.returncode
                result["force_output"] = (forced.stdout or forced.stderr or "").strip()[:500]
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["force_error"] = _safe_exception_text(exc)
    else:
        try:
            os.kill(pid, 15)
            await asyncio.sleep(max(grace_seconds, 0))
            if _is_pid_running(pid):
                os.kill(pid, 9)
                result["forced"] = True
        except OSError as exc:
            result["error"] = _safe_exception_text(exc)
    result["terminated"] = not _is_pid_running(pid)
    return result


async def _cleanup_registered_infra_processes(reason: str = "cleanup", dry_run: bool = False) -> dict[str, Any]:
    with app_core._INFRA_SPAWNED_PIDS_LOCK:
        pids = sorted(app_core._INFRA_SPAWNED_PIDS)
    items = []
    for pid in pids:
        kill_result = await _terminate_process_tree(
            pid,
            grace_seconds=PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS,
            dry_run=dry_run,
        )
        items.append({"reason": reason, **kill_result})
    if not dry_run:
        with app_core._INFRA_SPAWNED_PIDS_LOCK:
            app_core._INFRA_SPAWNED_PIDS.difference_update(pid for pid in pids if not _is_pid_running(pid))
    return {"reason": reason, "count": len(items), "items": items}


_HOOK_TRANSIT_ITEM_CHAR_LIMIT = 4000
_HOOK_SCRIPT_MODULE_CACHE: dict[str, Any] = {}
_HOOK_SCRIPT_MODULE_LOCK = threading.RLock()


def _hook_timestamp(request: BhmHookRequest) -> str:
    return request.timestamp or rt._utc_now_iso()


def _append_hook_observation(request: BhmHookRequest, endpoint: str) -> dict:
    ingress = ObservationIngressV1(
        schemaVersion=request.schemaVersion,
        eventId=request.eventId,
        hookType=request.hookType,
        sessionId=request.sessionId,
        correlationId=request.correlationId,
        parentEventId=request.parentEventId,
        project=request.project,
        cwd=request.cwd,
        timestamp=_hook_timestamp(request),
        source=request.source,
        endpoint=endpoint,
        payloadState=request.payloadState,
        sensitivity=request.sensitivity,
        data=dict(request.data or {}),
        metadata=dict(request.metadata or {}),
    )
    item = build_observation_record(ingress)
    app_core._append_observation(item)
    return item


def _hook_text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _hook_item_text(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        text = item.strip()
    else:
        try:
            text = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).strip()
        except TypeError:
            text = str(item).strip()
    return text[:_HOOK_TRANSIT_ITEM_CHAR_LIMIT]


def _with_context_tier_lifecycle_receipt(request: BhmHookRequest) -> tuple[BhmHookRequest, dict[str, Any]]:
    """Bind a content-free tier lifecycle receipt to the sanitized observation.

    This is intentionally metadata-only.  It creates no promotion lock and
    does not alter the legacy compact/idle behaviour beyond persisting an
    auditable lifecycle anchor with the existing observation event.
    """

    source_ids = app_core._lifecycle_source_ids(request)
    receipt = build_context_tier_lifecycle_receipt(
        project=request.project,
        session_id=request.sessionId,
        event_id=request.eventId,
        hook_type=request.hookType,
        parent_event_id=request.parentEventId,
        source_ids=source_ids,
    )
    metadata = dict(request.metadata or {})
    metadata["context_tier_lifecycle"] = receipt
    return request.model_copy(update={"metadata": metadata}), receipt


def _hook_compact_concepts(request: BhmHookCompactRequest) -> list[str]:
    concepts = [
        "bhm",
        "codex-hook",
        "pre-compact",
        "reflex",
        "crystallization",
    ]
    concepts.extend(app_core._coerce_hook_text_list(request.concepts))
    concepts.extend(app_core._coerce_hook_text_list(app_core._hook_data_value(request, "concepts", "tags")))
    return app_core._dedupe_text(concepts)


def _hook_compact_files(request: BhmHookCompactRequest) -> list[str]:
    files = app_core._coerce_hook_text_list(request.files)
    files.extend(app_core._coerce_hook_text_list(app_core._hook_data_value(request, "files", "files_touched", "filesTouched")))
    return app_core._dedupe_text(files)


def _materialize_hook_transit_sources(request: BhmHookCompactRequest, transit_items: list[Any]) -> list[dict]:
    records: list[dict] = []
    concepts = _hook_compact_concepts(request)
    files = _hook_compact_files(request)
    for app_ops.index, item in enumerate(transit_items[:app_core._HOOK_TRANSIT_SOURCE_LIMIT]):
        content = _hook_item_text(item)
        if not content:
            continue
        record_content = "\n".join(
            [
                "BHM pre-compact transit buffer:",
                f"hookType: {request.hookType}",
                f"sessionId: {request.sessionId}",
                f"project: {request.project}",
                f"cwd: {request.cwd}",
                f"index: {app_ops.index}",
                "payload:",
                content,
            ]
        )
        _, record = app_core._upsert_live_memory(
            MemoryUpsertRequest(
                upsert_key=f"hook-compact-source:{request.project}:{request.sessionId}:{app_ops.index}",
                project=request.project,
                type="transient-context",
                content=record_content,
                concepts=concepts,
                files=files,
            )
        )
        records.append(record)
    return records


def _build_hook_crystallize_request(request: BhmHookCompactRequest, source_ids: list[str]) -> MemoryCrystallizeRequest:
    title = (
        _hook_text_or_none(request.title)
        or _hook_text_or_none(app_core._hook_data_value(request, "title"))
        or f"{request.project} pre-compact reflex crystal"
    )
    summary = (
        _hook_text_or_none(request.summary)
        or _hook_text_or_none(app_core._hook_data_value(request, "summary", "reason"))
        or f"Emergency Codex pre-compact reflex captured {len(source_ids)} source item(s)."
    )
    upsert_key = (
        _hook_text_or_none(request.upsert_key)
        or _hook_text_or_none(app_core._hook_data_value(request, "upsert_key", "upsertKey"))
        or f"hook-compact-crystal:{request.project}:{request.sessionId}"
    )
    return MemoryCrystallizeRequest(
        source_ids=source_ids,
        project=request.project,
        title=title,
        summary=summary,
        target_type=request.target_type,
        concepts=_hook_compact_concepts(request),
        files=_hook_compact_files(request),
        upsert_key=upsert_key,
    )


def _handle_compact_hook(request: BhmHookCompactRequest) -> dict:
    request = app_core._ensure_hook_request_identity(request)
    request, tier_lifecycle = _with_context_tier_lifecycle_receipt(request)
    observation = _append_hook_observation(request, "compact")
    transit_items = app_core._hook_transit_items(request)
    source_ids = app_core._extract_hook_source_ids(request, transit_items)
    materialized_sources: list[dict] = []

    if not source_ids:
        materialized_sources = _materialize_hook_transit_sources(request, transit_items)
        source_ids = app_core._dedupe_text([str(record.get("source_id") or "") for record in materialized_sources])

    if not source_ids:
        return {
            "success": True,
            "action": "skipped",
            "reason": "empty_transit_buffer",
            "hook": {"type": request.hookType, "sessionId": request.sessionId, "project": request.project},
            "observation": {"id": observation.get("id")},
            "tier_lifecycle": tier_lifecycle,
        }

    action, record = app_core._crystallize_memories(_build_hook_crystallize_request(request, source_ids))
    return {
        "success": True,
        "action": action,
        "hook": {"type": request.hookType, "sessionId": request.sessionId, "project": request.project},
        "source_ids": source_ids,
        "materialized_source_ids": [record.get("source_id") for record in materialized_sources],
        "memory": app_core._serialize_memory_record(record),
        "observation": {"id": observation.get("id")},
        "tier_lifecycle": tier_lifecycle,
    }


def _apply_idle_duplicate_merge(request: BhmHookIdleRequest) -> dict:
    candidates = app_core._detect_duplicates(
        MemoryDetectRequest(project=request.project, limit=request.duplicate_limit, include_archived=False)
    )
    merged: list[dict[str, Any]] = []
    for candidate in candidates:
        if float(candidate.get("score") or 0.0) < 0.92:
            continue
        try:
            result = _merge_memories(
                MemoryMergeRequest(
                    project=request.project,
                    source_id=str(candidate.get("right_id") or ""),
                    target_id=str(candidate.get("left_id") or ""),
                    archive_source=True,
                )
            )
            merged.append(
                {
                    "source_id": (result.get("source") or {}).get("source_id"),
                    "target_id": (result.get("target") or {}).get("source_id"),
                    "score": candidate.get("score"),
                }
            )
        except Exception as exc:
            merged.append({"error": _safe_exception_text(exc), "candidate": candidate})
    return {"candidates": len(candidates), "merged": merged}


def _refresh_decay_scores_for_project(project: str, limit: int) -> dict:
    client = get_qdrant_client()
    now_dt = datetime.now(timezone.utc)
    recalculated_at = now_dt.isoformat().replace("+00:00", "Z")
    updated = 0
    scanned = 0
    errors: list[dict[str, str]] = []
    route = rt._project_projection_route(project)
    collection_names = [global_collection_name()]
    if route.collection_name is not None:
        collection_names.insert(0, route.collection_name)

    for collection_name in collection_names:
        offset = None
        while updated < limit:
            try:
                points, offset = client.scroll(
                    collection_name=collection_name,
                    limit=min(128, limit - updated),
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as exc:
                errors.append({"collection": collection_name, "error": _safe_exception_text(exc)})
                break
            if not points:
                break
            scanned += len(points)
            for point in points:
                payload = dict(getattr(point, "payload", None) or {})
                normalized = ensure_decay_metadata(
                    payload,
                    fallback_at=str(payload.get("updated_at") or payload.get("created_at") or recalculated_at),
                )
                raw_score = float(payload.get("score") or payload.get("raw_qdrant_score") or 1.0)
                decay_score = memory_decay_score(normalized, raw_qdrant_score=raw_score, now=now_dt)
                try:
                    client.set_payload(
                        collection_name=collection_name,
                        payload={
                            "importance_score": normalized["importance_score"],
                            "access_count": normalized["access_count"],
                            "last_accessed_at": normalized["last_accessed_at"],
                            "decay_score": decay_score,
                            "decay_recalculated_at": recalculated_at,
                        },
                        points=[getattr(point, "id")],
                    )
                    updated += 1
                except Exception as exc:
                    errors.append({"collection": collection_name, "point_id": str(getattr(point, "id", "")), "error": _safe_exception_text(exc)})
            if offset is None:
                break
        if updated >= limit:
            break
    return {"scanned": scanned, "updated": updated, "errors": errors}


def _repo_script_module(file_name: str, module_name: str) -> Any | None:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / file_name
    if not script_path.exists():
        return None
    with _HOOK_SCRIPT_MODULE_LOCK:
        cached = _HOOK_SCRIPT_MODULE_CACHE.get(file_name)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _HOOK_SCRIPT_MODULE_CACHE[file_name] = module
        return module


async def _run_idle_graph_healer(request: BhmHookIdleRequest) -> dict:
    module = _repo_script_module("bhm_graph_healer.py", "bhm_graph_healer_runtime")
    if module is None:
        return {"status": "skipped", "reason": "module_unavailable"}
    summary = await module.heal_graph(
        dry_run=not request.apply_graph_healer,
        max_orphans=request.max_orphans,
        max_links=request.max_links,
        rate_limit_ms=0,
    )
    return summary.to_dict() if hasattr(summary, "to_dict") else dict(summary)


async def _run_idle_reflection_daemon(request: BhmHookIdleRequest) -> dict:
    module = _repo_script_module("bhm_reflection_daemon.py", "bhm_reflection_daemon_runtime")
    if module is None:
        return {"status": "skipped", "reason": "module_unavailable"}
    return await module.run_reflection_cycle(
        project_name=request.project,
        dry_run=not request.apply_reflection,
        limit=request.reflection_limit,
        scan_limit=request.reflection_scan_limit,
        llm_timeout=float(request.reflection_timeout),
    )


async def _run_idle_reflection_pipeline(request: BhmHookIdleRequest) -> dict:
    request = app_core._ensure_hook_request_identity(request)
    request, tier_lifecycle = _with_context_tier_lifecycle_receipt(request)
    result: dict[str, Any] = {
        "success": True,
        "hook": {"type": request.hookType, "sessionId": request.sessionId, "project": request.project},
        "tier_lifecycle": tier_lifecycle,
        "started_at": rt._utc_now_iso(),
        "steps": {},
    }
    for step_name, step_call in (
        ("observe", lambda: _append_hook_observation(request, "idle")),
        ("duplicate_merge", lambda: _apply_idle_duplicate_merge(request)),
        ("decay_refresh", lambda: _refresh_decay_scores_for_project(request.project, request.decay_limit)),
    ):
        try:
            result["steps"][step_name] = await asyncio.to_thread(step_call)
        except Exception as exc:
            result["steps"][step_name] = {"success": False, "error": _safe_exception_text(exc)}

    for step_name, step_call in (
        ("graph_healer", lambda: _run_idle_graph_healer(request)),
        ("reflection_daemon", lambda: _run_idle_reflection_daemon(request)),
    ):
        try:
            result["steps"][step_name] = await step_call()
        except Exception as exc:
            result["steps"][step_name] = {"success": False, "error": _safe_exception_text(exc)}

    result["finished_at"] = rt._utc_now_iso()
    return result


def _merge_memories(request: MemoryMergeRequest) -> dict:
    if request.source_id == request.target_id:
        raise HTTPException(status_code=400, detail="source_id and target_id must differ")
    source = rt._find_live_memory(request.source_id, request.project)
    target = rt._find_live_memory(request.target_id, request.project)
    if source is None:
        raise HTTPException(status_code=404, detail="source memory not found")
    if target is None:
        raise HTTPException(status_code=404, detail="target memory not found")
    target, source = app_core._merge_memory_records(
        source,
        target,
        archive_source=request.archive_source,
        target_id=request.target_id,
    )
    app_core._save_live_memories([target, source])
    return {"target": target, "source": source, "archived_source": request.archive_source}


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
    _SEMANTIC_READINESS_GATE_ENABLED=lambda: app_core._SEMANTIC_READINESS_GATE_ENABLED,
    _fast_semantic_runtime_slo_status=lambda *a, **k: app_core._fast_semantic_runtime_slo_status(*a, **k),
    _observation_store=lambda *a, **k: app_core._observation_store(*a, **k),
    _read_git_head=lambda *a, **k: app_core._read_git_head(*a, **k),
    _semantic_readiness_receipt=lambda *a, **k: app_core._semantic_readiness_receipt(*a, **k),
    bhm_health_slo=lambda *a, **k: app_telemetry.bhm_health_slo(*a, **k),
    federated_search=lambda *a, **k: app_core.federated_search(*a, **k),
)


llm_routes.configure(
    _authorize_resource_project=lambda *a, **k: app_core._authorize_resource_project(*a, **k),
    _detect_conflicts=lambda *a, **k: app_ops._detect_conflicts(*a, **k),
    _detect_duplicates=lambda *a, **k: app_core._detect_duplicates(*a, **k),
    _llm_governor=lambda *a, **k: app_core._llm_governor(*a, **k),
    _memory_matches_filters=lambda *a, **k: app_core._memory_matches_filters(*a, **k),
    _relation_suggest=lambda *a, **k: app_ops._relation_suggest(*a, **k),
    federated_search=lambda *a, **k: app_core.federated_search(*a, **k),
)


galaxy_routes.configure(
    _memory_lifecycle=lambda *a, **k: app_core._memory_lifecycle(*a, **k),
    _memory_matches_filters=lambda *a, **k: app_core._memory_matches_filters(*a, **k),
    _require_typed_memory_boundary=lambda *a, **k: app_core._require_typed_memory_boundary(*a, **k),
)


ui_routes.configure(
    _read_boot_report=lambda *a, **k: app_core._read_boot_report(*a, **k),
)


memory_routes.configure(
    _RETRIEVAL_FUNNEL=lambda: app_core._RETRIEVAL_FUNNEL,
    _admit_new_content=lambda *a, **k: app_core._admit_new_content(*a, **k),
    _advanced_search_live_memories=lambda *a, **k: app_retrieval._advanced_search_live_memories(*a, **k),
    _append_memory_changelog=lambda *a, **k: app_core._append_memory_changelog(*a, **k),
    _artifact_store_pairs=lambda *a, **k: app_ops._artifact_store_pairs(*a, **k),
    _batch_mapping_items=lambda *a, **k: app_ops._batch_mapping_items(*a, **k),
    _batch_project_scope=lambda *a, **k: app_ops._batch_project_scope(*a, **k),
    _build_memory_title=lambda *a, **k: app_core._build_memory_title(*a, **k),
    _combined_review_status=lambda *a, **k: app_ops._combined_review_status(*a, **k),
    _create_memory_link=lambda *a, **k: app_ops._create_memory_link(*a, **k),
    _delete_live_memory=lambda *a, **k: app_ops._delete_live_memory(*a, **k),
    _delete_live_memory_hard=lambda *a, **k: app_ops._delete_live_memory_hard(*a, **k),
    _detect_conflicts=lambda *a, **k: app_ops._detect_conflicts(*a, **k),
    _detect_duplicates=lambda *a, **k: app_core._detect_duplicates(*a, **k),
    _fallback_grace_memories_response=lambda *a, **k: app_retrieval._fallback_grace_memories_response(*a, **k),
    _fetch_qdrant_hit_by_source_id=lambda *a, **k: app_core._fetch_qdrant_hit_by_source_id(*a, **k),
    _is_archived_memory=lambda *a, **k: app_core._is_archived_memory(*a, **k),
    _is_fallback_grace_error=lambda *a, **k: app_retrieval._is_fallback_grace_error(*a, **k),
    _lint_memory_record=lambda *a, **k: app_ops._lint_memory_record(*a, **k),
    _memory_content_sha256=lambda *a, **k: app_ops._memory_content_sha256(*a, **k),
    _memory_matches_filters=lambda *a, **k: app_core._memory_matches_filters(*a, **k),
    _memory_redact=lambda *a, **k: app_ops._memory_redact(*a, **k),
    _memory_review_queue=lambda *a, **k: app_ops._memory_review_queue(*a, **k),
    _merge_memories=lambda *a, **k: _merge_memories(*a, **k),
    _normalize_linked_dependencies=lambda *a, **k: app_hooks._normalize_linked_dependencies(*a, **k),
    _normalize_memory_metadata=lambda *a, **k: app_ops._normalize_memory_metadata(*a, **k),
    _project_matches=lambda *a, **k: app_core._project_matches(*a, **k),
    _record_age_days=lambda *a, **k: app_projects._record_age_days(*a, **k),
    _relation_suggest=lambda *a, **k: app_ops._relation_suggest(*a, **k),
    _replace_live_memory=lambda *a, **k: app_core._replace_live_memory(*a, **k),
    _request_actor=lambda *a, **k: app_core._request_actor(*a, **k),
    _request_ingress=lambda *a, **k: app_ops._request_ingress(*a, **k),
    _require_temporal_memory_boundary=lambda *a, **k: app_core._require_temporal_memory_boundary(*a, **k),
    _require_typed_memory_boundary=lambda *a, **k: app_core._require_typed_memory_boundary(*a, **k),
    _restore_archived_memory=lambda *a, **k: app_ops._restore_archived_memory(*a, **k),
    _review_queue_id=lambda *a, **k: app_ops._review_queue_id(*a, **k),
    _save_live_memories=lambda *a, **k: app_core._save_live_memories(*a, **k),
    _search_hit_collection=lambda *a, **k: app_core._search_hit_collection(*a, **k),
    _semantic_graph_node_id=lambda *a, **k: app_governed._semantic_graph_node_id(*a, **k),
    _serialize_conflict_candidate=lambda *a, **k: app_ops._serialize_conflict_candidate(*a, **k),
    _serialize_memory_link=lambda *a, **k: app_ops._serialize_memory_link(*a, **k),
    _serialize_memory_record=lambda *a, **k: app_core._serialize_memory_record(*a, **k),
    _set_memory_pin=lambda *a, **k: app_projects._set_memory_pin(*a, **k),
    _strict_retrieval_hits=lambda *a, **k: app_core._strict_retrieval_hits(*a, **k),
    _sync_mem0_record=lambda *a, **k: app_core._sync_mem0_record(*a, **k),
    _update_live_memory=lambda *a, **k: app_core._update_live_memory(*a, **k),
    _upsert_live_memory=lambda *a, **k: app_core._upsert_live_memory(*a, **k),
    _user_memory_metadata=lambda *a, **k: app_core._user_memory_metadata(*a, **k),
    federated_search=lambda *a, **k: app_core.federated_search(*a, **k),
)
