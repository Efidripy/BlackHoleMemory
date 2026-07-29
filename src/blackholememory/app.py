from __future__ import annotations

import time
import asyncio
import base64
import ctypes
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import subprocess
import sqlite3
import threading
import urllib.error
from urllib.parse import urlsplit
import urllib.request
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping

import httpx
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import StrictBool
from pydantic import StrictInt
from pydantic import StrictStr
from qdrant_client.http import models as qdrant_models
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from .config import settings
from .llm_gateway import GatewayRequest
from .llm_gateway import GATEWAY_SCHEMA_VERSION
from .llm_gateway import LocalLLMGateway
from .llm_gateway import LocalOpenAICompatibleAdapter
from .llm_gateway import ModelDefinition
from .llm_gateway import ModelRegistry
from .llm_gateway import PromptDefinition
from .llm_gateway import PromptRegistry
from .capability import ADMIN_CAPABILITY_HEADER
from .capability import admin_route_requires_capability
from .capability import configured_admin_capability
from .capability import extract_mcp_capability
from .capability import is_admin_capability_valid
from .caller_auth import authorize_projects
from .caller_auth import caller_auth_configuration_error
from .caller_auth import caller_route_policy
from .caller_auth import CallerRoutePolicy
from .caller_auth import configured_caller_principal
from .caller_auth import extract_request_projects
from .caller_auth import is_caller_token_valid
from .caller_auth import MAX_PROJECT_INSPECTION_BYTES
from .caller_auth import parse_bearer_token
from .ui_session import BOOTSTRAP_TTL_SECONDS
from .ui_session import SESSION_TTL_SECONDS
from .ui_session import UI_SESSION_COOKIE
from .ui_session import UiSessionRegistry
from .ui_session import ui_session_route_allowed
from .embedding_reuse import search_with_precomputed_embedding
from .embedding_cache import EmbeddingCache
from .embedding_cache import embed_query_with_cache
from .graph import build_graph
from .galaxy import GalaxyOptions
from .galaxy import build_galaxy_graph
from .galaxy import camera_distance_for
from .health import dependency_report
from .health_contract import bhm_health_payload
from .health_contract import health_cutover_payload
from .health_contract import health_live_payload
from .health_contract import health_ready_payload
from .health_contract import health_slo_payload
from .infra.mcp_broker import _BHM_REMEMBER_ALLOWED_ARGUMENTS
from .mem0_adapter import BHMGraphManager
from .mem0_adapter import StorageNotReady
from .mem0_adapter import decay_lambda_for_payload
from .mem0_adapter import ensure_memory_collections
from .mem0_adapter import ensure_decay_metadata
from .mem0_adapter import get_global_core_memory
from .mem0_adapter import get_project_mem0_memory
from .mem0_adapter import get_qdrant_client
from .mem0_adapter import global_collection_name
from .mem0_adapter import initial_decay_metadata
from .mem0_adapter import lexical_score
from .mem0_adapter import local_collection_name
from .mem0_adapter import memory_decay_score
from .mem0_adapter import storage_runtime_state
from .mem0_adapter import mem0_runtime_plan
from .mem0_adapter import normalize_access_count
from .mem0_adapter import normalize_importance_score
from .mcp_surfaces import CORE_TOOL_NAMES
from .mcp_surfaces import filter_tools
from .mcp_surfaces import is_tool_allowed
from .mcp_surfaces import requires_admin_capability
from .mcp_surfaces import resolve_mcp_surface
from .mcp_protocol_contract import initialize_capabilities
from .mcp_protocol_contract import negotiate_protocol_version
from .mcp_protocol_contract import ProtocolContractError
from .mcp_streamable_http import BhmStreamableHttpGateway
from .openapi_contract import build_openapi_schema
from .project_registry import ProjectResolution
from .project_registry import get_default_project_registry
from .project_retirement import ProjectRetirementError
from .project_retirement import apply_project_retirement
from .project_retirement import preview_project_retirement
from .retrieval_filters import build_candidate_filters
from .retrieval_fusion import weighted_rank_fusion
from .retrieval_diversity import mmr_select
from .semantic_observation import build_semantic_observation
from .semantic_relevance_receipt import build_semantic_relevance_receipt
from .semantic_fusion_provenance_receipt import build_semantic_fusion_provenance_receipt
from .semantic_readiness import SemanticReadinessCache
from .semantic_readiness import build_readiness_key
from .semantic_readiness import evaluate_semantic_readiness
from .semantic_readiness import project_warmup_state
from .semantic_code_search import SemanticCodeSearchError
from .semantic_code_search import semantic_search_metadata
from .context_compiler import MAX_CONTEXT_TOKEN_BUDGET
from .context_compiler import compile_context
from .context_profiles import resolve_context_profile
from .context_profiles import load_context_profiles
from .adaptive_profile import recommend_context_profile
from .adaptive_profile import summarize_explicit_usefulness
from .context_confidence import assess_context_confidence
from .lifecycle_suggestions import build_lifecycle_suggestions
from .feedback_tuning import build_feedback_tuning
from .feedback_tuning import summarize_quality_feedback
from .retrieval_explanation import explain_retrieval_hit
from .runtime_storage import MemoryStoreMode
from .runtime_storage import runtime_storage_state as memory_runtime_storage_state
from .runtime_storage import resolve_runtime_storage_config
from .memory_service import MemoryServiceNotReady
from .memory_service import MemoryServiceValidationError
from .memory_service import SQLiteMemoryService
from .sync_service import InvalidTombstone
from .sync_service import UndoWindowExpired
from .vector_routing import route_vector_targets
from .version_manifest import BROKER_VERSION
from .version_manifest import RUNTIME_VERSION
from .hook_queue import HookJobCollision
from .hook_queue import HookJobLeaseLost
from .hook_queue import HookJobQueue
from .hook_queue import HookQueueError
from .hook_queue import HookQueueFull
from .observation_contract import ObservationIngressV1
from .observation_contract import build_observation_record
from .observation_store import ObservationIdCollision
from .observation_store import ObservationStore
from .retention import build_retention_plan
from .retention import load_retention_policy
from .retention import parse_timestamp
from .retention import RetentionPolicyError
from .retention import summarize_retention_plan
from .observation_security import OBSERVATION_COMPACT_MAX_INPUT_BYTES
from .observation_security import OBSERVATION_IDLE_MAX_INPUT_BYTES
from .observation_security import OBSERVATION_MAX_INPUT_BYTES
from .observation_security import ObservationPayloadTooLarge
from .observation_security import contains_secret_like
from .observation_security import observation_body_limit
from .observation_security import redact_secret_text
from .observation_security import secure_observation_payload
from .usage_telemetry import UsageTelemetry
from .usage_telemetry import monotonic_elapsed_ms
from .usage_telemetry import normalize_operation
from .retrieval_funnel import RetrievalFunnel
from .llm_job_queue import LLM_JOB_QUEUE_SCHEMA_VERSION
from .llm_job_queue import LLMJobIdempotencyCollision
from .llm_job_queue import LLMJobQueue
from .llm_job_queue import LLMJobQueueError
from .llm_job_queue import LLMJobQueueFull
from .llm_job_queue import default_llm_job_queue_path
from .llm_job_queue import deterministic_llm_job_id
from .llm_long_tasks import LLM_LONG_TASK_MAX_CHUNKS
from .llm_long_tasks import LLM_LONG_TASK_MAX_FANOUT
from .llm_long_tasks import LLM_LONG_TASK_PLAN_VERSION
from .llm_long_tasks import LongTaskStore
from .llm_long_tasks import default_long_task_store_path
from .llm_candidates import LLM_CANDIDATE_JUDGE_VERSION
from .llm_candidates import LLM_CANDIDATE_MAX
from .llm_candidates import LLM_CANDIDATE_ROLES
from .llm_candidates import LLM_CANDIDATE_SCHEMA_VERSION
from .llm_candidates import CandidateError
from .llm_candidates import build_candidate_plan
from .safe_patch_factory import SAFE_PATCH_MAX_DIFF_BYTES
from .safe_patch_factory import SAFE_PATCH_MAX_FILES
from .safe_patch_factory import SAFE_PATCH_MAX_TIMEOUT_SECONDS
from .safe_patch_factory import SAFE_PATCH_SCHEMA_VERSION
from .llm_delegation_policy import LLM_DELEGATION_POLICY_VERSION
from .llm_delegation_policy import DelegationPolicyError
from .llm_delegation_policy import decide_delegation
from .llm_delegation_policy import delegation_policy_snapshot
from .memory_foundry import MEMORY_FOUNDRY_MAX_RECORDS
from .memory_foundry import MEMORY_FOUNDRY_SCHEMA_VERSION
from .memory_foundry import MemoryFoundryError
from .memory_foundry import build_memory_foundry_preview
from .retrieval_lab import RETRIEVAL_LAB_FEATURES
from .retrieval_lab import RETRIEVAL_LAB_MAX_BENCHMARK_CASES
from .retrieval_lab import RETRIEVAL_LAB_SCHEMA_VERSION
from .retrieval_lab import RetrievalLabError
from .retrieval_lab import build_retrieval_lab_preview
from .repository_intelligence import REPOSITORY_INTELLIGENCE_MAX_FILES
from .repository_intelligence import REPOSITORY_INTELLIGENCE_SCHEMA_VERSION
from .repository_intelligence import RepositoryIntelligenceError
from .repository_intelligence import build_repository_intelligence_preview
from .repository_intelligence import collect_repository_files
from .architecture_intelligence import build_architecture_intelligence
from .architecture_intelligence import build_architecture_explain_receipt
from .architecture_intelligence import build_architecture_memory
from .architecture_intelligence import build_graph_analysis_quality_receipt
from .code_graph_query import ALLOWED_OPERATIONS as CODE_GRAPH_QUERY_OPERATIONS
from .code_graph_query import CodeGraphQueryError
from .code_graph_query import explain_code_graph
from .code_graph_query import query_code_graph
from .code_graph import CODE_GRAPH_EXTRACTOR_VERSION
from .code_graph import CODE_GRAPH_SCHEMA_VERSION
from .code_graph import CodeGraphError
from .code_graph import PARSER_REGISTRY_DIGEST
from .code_graph import LANGUAGE_INVENTORY_DIGEST
from .code_graph import parser_capability_matrix
from .code_graph import SQLiteCodeGraphStore
from .code_graph import build_code_graph
from .code_graph_artifact import CodeGraphArtifactError
from .code_graph_artifact import CODE_GRAPH_ARTIFACT_SCHEMA_VERSION
from .code_graph_artifact import export_graph_artifact
from .code_graph_artifact import build_graph_artifact_promotion_plan
from .code_graph_artifact import verify_graph_artifact
from .code_graph_dsl import GraphDslError
from .code_graph_dsl import query_graph_dsl
from .cross_repo_links import build_cross_repo_link_preview
from .change_impact import ChangeImpactError
from .change_impact import build_change_impact_preview
from .change_impact import build_impact_binding_receipt
from .change_impact import build_git_history_correlation_receipt
from .change_impact_risk_receipt import build_change_impact_risk_receipt
from .change_impact import collect_git_change_paths
from .change_impact import collect_git_diff_hunks
from .change_impact import collect_git_history_stats
from .change_impact import correlate_diff_hunks_to_symbols
from .change_impact import correlate_git_history_to_symbols
from .git_history_test_receipt import build_commit_symbol_test_history_receipt
from .convention_memory import ConventionMemoryError
from .convention_memory import preview_convention_memory
from .unified_context import UnifiedContextError
from .unified_context import build_unified_context_from_graph
from .unified_context import classify_context_item
from .session_capture import DISCLOSURE_LEVELS
from .session_capture import SessionCaptureError
from .session_capture import build_session_capture_preview
from .memory_graph import MEMORY_GRAPH_OPERATIONS
from .memory_graph import MemoryGraphError
from .memory_graph import explain_memory_graph
from .memory_graph import query_memory_graph
from .task_graph import TASK_GRAPH_OPERATIONS
from .task_graph import TaskGraphError
from .task_graph import explain_task_graph
from .task_graph import query_task_graph
from .llm_code_fabric import LLM_CODE_FABRIC_TASKS
from .llm_code_fabric import LLMCodeFabricError
from .llm_code_fabric import build_code_fabric_plan
from .factory_integration import FACTORY_INTEGRATION_MAX_ITEMS
from .factory_integration import FactoryIntegrationError
from .factory_integration import build_factory_integration_preview
from .unified_mcp_contract import UnifiedMcpContractError
from .unified_mcp_contract import build_unified_mcp_contract
from .capability_router import CapabilityRouterError
from .capability_router import build_capability_route_plan
from .human_ui_bridge import HumanUiBridgeError
from .human_ui_bridge import build_human_ui_bridge_preview
from .migration_compatibility import MigrationCompatibilityError
from .migration_compatibility import build_migration_preview
from .security_trust_boundary import SecurityTrustBoundaryError
from .security_trust_boundary import build_security_trust_boundary_preview
from .security_boundaries import SecurityBoundaryError
from .security_boundaries import compile_bounded_regex
from .security_boundaries import resolve_under_root
from .trace_graph import TraceGraphError
from .trace_graph import build_trace_graph
from .trace_graph import validate_trace_graph
from .service_trace_receipt import SERVICE_TRACE_RECEIPT_SCHEMA_VERSION
from .service_trace_receipt import build_service_trace_receipt
from .repository_index import RepositoryIndexError
from .repository_index import DEFAULT_WATCH_MAX_INFLIGHT_JOBS
from .repository_index import MAX_WATCH_MAX_INFLIGHT_JOBS
from .repository_index import RepositorySourceProvenance
from .repository_index import RepositoryWatcher
from .repository_index import SQLiteRepositoryIndexStore
from .repository_index import index_repository
from .code_search import CodeSearchError
from .code_search import get_repository_snippet
from .code_search import search_repository_code
from .code_search import semantic_fusion_enabled
from .code_search import fuse_code_search_matches
from .package_resolution import PACKAGE_RESOLUTION_SCHEMA_VERSION
from .package_resolution import DEPENDENCY_PROVENANCE_SCHEMA_VERSION
from .package_resolution import DependencyProvenanceError
from .package_resolution import PackageResolutionError
from .package_resolution import resolve_dependency_provenance
from .package_resolution import resolve_package_manifests
from .dependency_provenance_receipt import build_dependency_provenance_receipt
from .package_resolution_receipt import build_package_resolution_receipt
from .type_reference_resolution import TYPE_REFERENCE_RESOLUTION_SCHEMA_VERSION
from .type_reference_resolution import build_type_reference_resolution
from .resolution_quality_receipt import build_resolution_quality_receipt
from .bicep_module_resolution import BICEP_MODULE_RESOLUTION_SCHEMA_VERSION
from .bicep_module_resolution import build_bicep_module_resolution
from .repository_index import probe_repository_state
from .repository_index import repository_index_status
from .qa_incident_factory import QA_INCIDENT_FEATURES
from .qa_incident_factory import QA_INCIDENT_MAX_ARTIFACTS
from .qa_incident_factory import QA_INCIDENT_SCHEMA_VERSION
from .qa_incident_factory import QAIncidentFactoryError
from .qa_incident_factory import build_qa_incident_preview
from .documentation_factory import DOCUMENTATION_FACTORY_FEATURES
from .documentation_factory import DOCUMENTATION_FACTORY_MAX_DOCUMENTS
from .documentation_factory import DOCUMENTATION_FACTORY_SCHEMA_VERSION
from .documentation_factory import DocumentationFactoryError
from .documentation_factory import build_documentation_factory_preview
from .night_shift import NIGHT_SHIFT_SAFE_JOB_TYPES
from .night_shift import NIGHT_SHIFT_SCHEMA_VERSION
from .night_shift import NightShiftError
from .night_shift import build_night_shift_preview
from .model_router import MODEL_ROUTER_CAPABILITIES
from .model_router import MODEL_ROUTER_CONTEXT_PROFILES
from .model_router import MODEL_ROUTER_SCHEMA_VERSION
from .model_router import ModelRouterError
from .model_router import route_model
from .model_router import router_snapshot
from .llm_cache import LLM_CACHE_DEFAULT_TTL_SECONDS
from .llm_cache import LLM_CACHE_MAX_ENTRIES
from .llm_cache import LLM_CACHE_POLICY_VERSION
from .llm_cache import LLMCacheError
from .llm_cache import LLMCacheStore
from .llm_cache import build_cache_identity
from .llm_cache import build_cache_preview
from .llm_cache import default_llm_cache_path
from .llm_learning import LLM_LEARNING_MAX_DATASET_RECORDS
from .llm_learning import LLM_LEARNING_MAX_RECORDS
from .llm_learning import LLM_LEARNING_POLICY_VERSION
from .llm_learning import LLMLearningBoundsError
from .llm_learning import LLMLearningCollision
from .llm_learning import LLMLearningError
from .llm_learning import LLMLearningPrivacyError
from .llm_learning import LLMLearningReviewError
from .llm_learning import LLMLearningStore
from .llm_learning import LLMLearningStoreFull
from .llm_learning import default_llm_learning_path
from .llm_resource_governor import AdmissionRequest
from .llm_resource_governor import GovernorConfig
from .llm_resource_governor import LLMResourceGovernor
from .llm_resource_governor import LLMResourceGovernorError
from .llm_safety import LLM_SAFETY_POLICY_VERSION
from .llm_safety import LLMSafetyViolation
from .llm_safety import PROPOSAL_AUTHORITY
from .llm_safety import build_proposal_envelope
from .llm_safety import sanitize_llm_value
from .llm_safety import scan_prompt_injection
from .llm_telemetry import get_llm_telemetry
from .mcp_panel import build_mcp_panel_snapshot
from .mcp_panel import load_configured_sources
from .mcp_repair import McpRepairError
from .mcp_repair import build_repair_preview
from .mcp_repair import build_reprobe
from .mcp_repair import execute_reconnect
from .mcp_repair import execute_rollback
from .surface_report import build_surface_report
from .qdrant_catalog import build_qdrant_catalog


_INFRA_SPAWNED_PIDS: set[int] = set()
_INFRA_SPAWNED_PIDS_LOCK = threading.RLock()


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        return max(default, minimum)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(float(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        return max(default, minimum)


def _env_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().casefold() in {"1", "true", "yes", "on"}


_MAX_CONCURRENT_WRITES = _env_int("BHM_MAX_CONCURRENT_WRITES", 10, 1)
_WRITE_QUEUE_LIMIT = _env_int("BHM_WRITE_QUEUE_LIMIT", 20, 0)
_WRITE_RETRY_AFTER_SECONDS = _env_int("BHM_WRITE_RETRY_AFTER_SECONDS", 5, 1)
_WRITE_ACQUIRE_TIMEOUT_SECONDS = _env_float("BHM_WRITE_ACQUIRE_TIMEOUT_SECONDS", 1.0, 0.0)
_WRITE_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_WRITES)
_WRITE_BACKPRESSURE_LOCK = asyncio.Lock()
_WRITE_BACKPRESSURE_ACTIVE = 0
_WRITE_BACKPRESSURE_WAITING = 0
_HOOK_QUEUE_CAPACITY = _env_int("BHM_HOOK_QUEUE_CAPACITY", 128, 1)
_HOOK_QUEUE_MAX_ATTEMPTS = _env_int("BHM_HOOK_QUEUE_MAX_ATTEMPTS", 3, 1)
_HOOK_COMPACT_WORKERS = _env_int("BHM_HOOK_COMPACT_WORKERS", 1, 1)
_HOOK_IDLE_WORKERS = _env_int("BHM_HOOK_IDLE_WORKERS", 1, 1)
_HOOK_QUEUE_RETRY_AFTER_SECONDS = _env_int("BHM_HOOK_QUEUE_RETRY_AFTER_SECONDS", 2, 1)
_HOOK_QUEUE_LEASE_SECONDS = _env_float("BHM_HOOK_QUEUE_LEASE_SECONDS", 120.0, 5.0)
_HOOK_QUEUE_POLL_SECONDS = _env_float("BHM_HOOK_QUEUE_POLL_SECONDS", 0.05, 0.01)
_HOOK_QUEUE_DRAIN_SECONDS = _env_float("BHM_HOOK_QUEUE_DRAIN_SECONDS", 10.0, 0.1)
_HOOK_QUEUE_RETRY_BASE_SECONDS = _env_float("BHM_HOOK_QUEUE_RETRY_BASE_SECONDS", 1.0, 0.0)
_HOOK_QUEUE_BOOT_ID = f"bhm-hook-worker-{uuid.uuid4().hex[:12]}"
_HOOK_QUEUE_STORES: dict[str, HookJobQueue] = {}
_HOOK_QUEUE_STORES_LOCK = threading.RLock()
_HOOK_QUEUE_TASKS: list[asyncio.Task[None]] = []
_HOOK_QUEUE_STOP_EVENT: asyncio.Event | None = None
_HOOK_QUEUE_ACCEPTING = True
_STORAGE_STARTUP_TIMEOUT_SECONDS = _env_float("BHM_STORAGE_STARTUP_TIMEOUT_SECONDS", 30.0, 0.1)
_QDRANT_HEALTH_TIMEOUT_SECONDS = _env_float("BHM_QDRANT_HEALTH_TIMEOUT_SECONDS", 2.0, 0.1)
_FALLBACK_MODE_ENV = "BHM_FALLBACK_MODE"
_TELEMETRY_INTERVAL_SECONDS = 2.5
_FALLBACK_GRACE_ACTIVE_UNTIL = 0.0
_CUSTOM_REDACTION_MAX_PATTERNS = 16
_CUSTOM_REDACTION_MAX_PATTERN_LENGTH = 120
_CUSTOM_REDACTION_MAX_INPUT_CHARS = 64 * 1024
_BOOT_REPORT_PATH = settings.runtime_dir / "infra" / "boot_report.json"
_BOOT_REPORT_QDRANT_POLL_SECONDS = 0.25
_WINDOWS_DETACHED_PROCESS = 0x00000008
_WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WINDOWS_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_WINDOWS_CREATE_NO_WINDOW = 0x08000000


class ResponseTimeout(Exception):
    """Local transport/readiness timeout that should fall back to disk snapshots."""


_PROVIDER_WARMUP_REQUIRED = os.getenv("BHM_PROVIDER_WARMUP_DISABLED", "").lower() not in {"1", "true", "yes"}
_PROVIDER_WARMUP_TIMEOUT_SECONDS = _env_float("BHM_PROVIDER_WARMUP_TIMEOUT_SECONDS", 5.0, 0.1)
_PROVIDER_WARMUP_INITIAL_DELAY_SECONDS = _env_float("BHM_PROVIDER_WARMUP_INITIAL_DELAY_SECONDS", 1.0, 0.1)
_PROVIDER_WARMUP_MAX_DELAY_SECONDS = _env_float("BHM_PROVIDER_WARMUP_MAX_DELAY_SECONDS", 30.0, 1.0)
_PROVIDER_READINESS_WAIT_SECONDS = _env_float("BHM_PROVIDER_READINESS_WAIT_SECONDS", 5.0, 0.0)
_PROVIDER_EMBEDDING_WARMUP_ENABLED = os.getenv("BHM_PROVIDER_EMBEDDING_WARMUP", "").lower() in {
    "1",
    "true",
    "yes",
}
_PROVIDER_EMBEDDING_WARMUP_TIMEOUT_SECONDS = _env_float(
    "BHM_PROVIDER_EMBEDDING_WARMUP_TIMEOUT_SECONDS",
    _PROVIDER_WARMUP_TIMEOUT_SECONDS,
    0.1,
)
_PROVIDER_EMBEDDING_WARMUP_ATTEMPTS = _env_int("BHM_PROVIDER_EMBEDDING_WARMUP_ATTEMPTS", 2, 1)
_PROVIDER_EMBEDDING_WARMUP_RETRY_DELAY_SECONDS = _env_float(
    "BHM_PROVIDER_EMBEDDING_WARMUP_RETRY_DELAY_SECONDS",
    0.25,
    0.0,
)
_PROVIDER_MEMORY_WARMUP_ENABLED = os.getenv("BHM_PROVIDER_MEMORY_WARMUP", "").lower() in {
    "1",
    "true",
    "yes",
}
_PROVIDER_MEMORY_WARMUP_MAX_PROJECTS = _env_int("BHM_PROVIDER_MEMORY_WARMUP_MAX_PROJECTS", 8, 1)
_FACT_SYNTHESIS_TIMEOUT_SECONDS = _env_float("BHM_FACT_SYNTHESIS_TIMEOUT_SECONDS", 20.0, 0.1)
_FACT_SYNTHESIS_MAX_ZONE_ITEMS = _env_int("BHM_FACT_SYNTHESIS_MAX_ZONE_ITEMS", 80, 1)
_FACT_SYNTHESIS_MAX_ITEM_CHARS = _env_int("BHM_FACT_SYNTHESIS_MAX_ITEM_CHARS", 1600, 200)
_FACT_SYNTHESIS_MAX_TOKENS = _env_int("BHM_FACT_SYNTHESIS_MAX_TOKENS", 800, 100)
_EMBEDDING_CACHE_MAX_ENTRIES = _env_int("BHM_EMBEDDING_CACHE_MAX_ENTRIES", 256, 1)
_EMBEDDING_CACHE_TTL_SECONDS = _env_float("BHM_EMBEDDING_CACHE_TTL_SECONDS", 300.0, 0.1)
_QUERY_EMBEDDING_CACHE = EmbeddingCache(
    max_entries=_EMBEDDING_CACHE_MAX_ENTRIES,
    ttl_seconds=_EMBEDDING_CACHE_TTL_SECONDS,
)
_USAGE_TELEMETRY = UsageTelemetry()
_RETRIEVAL_FUNNEL = RetrievalFunnel()
_LLM_JOB_QUEUE_CAPACITY = _env_int("BHM_LLM_JOB_QUEUE_CAPACITY", 128, 1)
_LLM_JOB_QUEUE = LLMJobQueue(default_llm_job_queue_path(), capacity=_LLM_JOB_QUEUE_CAPACITY)
_LLM_LONG_TASK_STORE = LongTaskStore(default_long_task_store_path())
_LLM_CACHE_STORE = LLMCacheStore(default_llm_cache_path())
_LLM_LEARNING_STORE = LLMLearningStore(default_llm_learning_path())
_LLM_GOVERNOR_LOCK = threading.RLock()
_LLM_GOVERNOR: LLMResourceGovernor | None = None
_PROVIDER_WARMUP_READY = threading.Event()
_PROVIDER_WARMUP_STATUS_LOCK = threading.RLock()
_PROVIDER_WARMUP_STATUS: dict[str, Any] = {
    "enabled": _PROVIDER_WARMUP_REQUIRED,
    "ready": not _PROVIDER_WARMUP_REQUIRED,
    "attempts": 0,
    "last_error": "",
    "updated_at": "",
    "embedding_warmup_enabled": _PROVIDER_EMBEDDING_WARMUP_ENABLED,
    "embedding_ready": not _PROVIDER_EMBEDDING_WARMUP_ENABLED,
    "embedding_attempts": 0,
    "embedding_last_error": "",
    "embedding_phase": "disabled" if not _PROVIDER_EMBEDDING_WARMUP_ENABLED else "pending",
    "memory_warmup_enabled": _PROVIDER_MEMORY_WARMUP_ENABLED,
    "memory_ready": not _PROVIDER_MEMORY_WARMUP_ENABLED,
    "memory_projects": [],
    "memory_skipped_projects": [],
    "memory_last_error": "",
    "memory_phase": "disabled" if not _PROVIDER_MEMORY_WARMUP_ENABLED else "pending",
}
_VECTOR_CONTEXT_LOCAL = "LOCAL"
_VECTOR_CONTEXT_GLOBAL = "GLOBAL"
_GRAPH_EXPANSION_EDGE_TYPES = ["DEPENDS_ON", "UPGRADES"]
_SEMANTIC_LINK_EDGE_TYPES = {"DEPENDS_ON", "UPGRADES", "CONTRADICTS"}
_GRAPH_FUSION_WEIGHT = 0.04
_MMR_LAMBDA = min(_env_float("BHM_MMR_LAMBDA", 0.94, 0.0), 1.0)
_SEMANTIC_READINESS_GATE_ENABLED = _env_enabled("BHM_SEMANTIC_READINESS_GATE", False)
_SEMANTIC_READINESS_CACHE = SemanticReadinessCache(
    ttl_seconds=_env_float("BHM_SEMANTIC_READINESS_CACHE_TTL_SECONDS", 30.0, 1.0)
)
_BHM_GRAPH_MANAGER = BHMGraphManager()
FACT_SYNTHESIS_SYSTEM_PROMPT = """Вы — Ядро Дистилляции Знаний (Knowledge Synthesis Engine).
Ваша задача — принять трехзонный контекст инженерной сессии и превратить его в один эталонный Кристалл Факта (Fact Crystal).

Вам предоставлены зоны логов:
1. Active (Сырые детали инцидента)
2. Compress (Агрегированные сигнатуры циклических сбоев)
3. Frozen (Исторические вехи и чекпоинты)

Вырежьте весь временный мусор. Сформируйте строгий JSON со следующими полями:
- core_insight: <главный архитектурный вывод задачи>
- root_cause_resolved: <какая первопричина дефекта была устранена>
- reusable_patterns: <массив переиспользуемых паттернов кодинга/команд для всей машины>
- tags: <массив технологических тегов: FastAPI, Qdrant, Docker и т.д.>"""

FACT_SYNTHESIS_SYSTEM_PROMPT = (
    FACT_SYNTHESIS_SYSTEM_PROMPT
    + """

Enterprise taxonomy rules:
- You are an Enterprise Data Architect. When analyzing logs, you MUST determine domain, priority, and semantic_type for the resulting knowledge.
- Return domain as one of: frontend, backend, infra, security, product, general.
- Return priority as one of: low, medium, high, critical.
- Return semantic_type as one of: architecture, bugfix, feature, refactor, knowledge.
- Docker, WSL, PowerShell, Qdrant, Mem0, MCP, runtime, worker, ports, deploy, or local infrastructure issues => domain=infra.
- UI, interface, colors, layout, canvas, Three.js, HTML/CSS, React, screenshots, or visual behavior => domain=frontend.
- API routes, FastAPI, service logic, persistence adapters, or backend contracts => domain=backend.
- secrets, tokens, auth, permissions, vulnerabilities, or hard-delete/sensitive operations => domain=security.
- roadmap, user workflow, requirements, product behavior, or acceptance criteria => domain=product.
- errors, crashes, failed checks, timeouts, regressions, tracebacks, or broken validation => priority=high unless data loss/security/critical outage requires priority=critical.
- architecture decisions or cross-component contracts => semantic_type=architecture.
- resolved failures, regressions, and root-cause fixes => semantic_type=bugfix.
- new capability delivery => semantic_type=feature.
- restructuring without new behavior => semantic_type=refactor.
- durable operating knowledge or reusable guidance => semantic_type=knowledge.
"""
)

if not _PROVIDER_WARMUP_REQUIRED:
    _PROVIDER_WARMUP_READY.set()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _set_provider_warmup_status(**patch: Any) -> None:
    with _PROVIDER_WARMUP_STATUS_LOCK:
        _PROVIDER_WARMUP_STATUS.update(patch)
        _PROVIDER_WARMUP_STATUS["updated_at"] = _utc_now_iso()


def _get_provider_warmup_status() -> dict[str, Any]:
    with _PROVIDER_WARMUP_STATUS_LOCK:
        status = dict(_PROVIDER_WARMUP_STATUS)
    status["ready"] = _PROVIDER_WARMUP_READY.is_set()
    return status


def _provider_warmup_url() -> str:
    explicit = os.getenv("BHM_PROVIDER_WARMUP_URL", "").strip()
    if explicit:
        return explicit
    endpoint = os.getenv("BHM_PROVIDER_WARMUP_ENDPOINT", "chat/completions").strip().lstrip("/")
    return f"{settings.mem0_openai_base_url.rstrip('/')}/{endpoint}"


def _post_provider_warmup_probe() -> None:
    payload = {
        "model": settings.mem0_llm_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    # Qwen models served by LM Studio may spend the warmup budget in their
    # reasoning phase unless thinking is explicitly disabled.  Keep this
    # provider-specific compatibility hint bounded to Qwen (or an explicit
    # operator override) so generic OpenAI-compatible endpoints retain their
    # normal request shape.
    disable_thinking = os.getenv("BHM_PROVIDER_DISABLE_THINKING", "").strip().lower()
    if disable_thinking in {"1", "true", "yes", "on"} or "qwen" in settings.mem0_llm_model.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json"}
    if settings.mem0_api_key:
        headers["Authorization"] = f"Bearer {settings.mem0_api_key}"
    request = urllib.request.Request(
        _provider_warmup_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_PROVIDER_WARMUP_TIMEOUT_SECONDS) as response:
        response.read(128)


def _post_provider_embedding_warmup_probe() -> None:
    """Load the configured embedding model without returning or persisting vectors.

    This is deliberately an opt-in startup probe, enabled only by the explicit
    semantic-fusion launcher switch.  A tiny local request removes the model's
    first-use latency from the first operator-requested semantic query while
    preserving SQLite authority and the projection-only boundary.
    """
    payload = {
        "model": settings.mem0_embedding_model,
        "input": ["bhm semantic fusion warmup"],
        "encoding_format": "float",
    }
    headers = {"Content-Type": "application/json"}
    if settings.mem0_api_key:
        headers["Authorization"] = f"Bearer {settings.mem0_api_key}"
    request = urllib.request.Request(
        f"{settings.mem0_openai_base_url.rstrip('/')}/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_PROVIDER_EMBEDDING_WARMUP_TIMEOUT_SECONDS) as response:
        # Do not expose or retain the returned embedding vector.  Reading a
        # bounded prefix is enough to let urllib close the response cleanly.
        response.read(128)


def _provider_memory_warmup_projects() -> list[str]:
    raw = os.getenv("BHM_PROVIDER_WARMUP_PROJECTS", "blackholememory")
    projects: list[str] = []
    for value in raw.split(","):
        project = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
        if project and project not in projects:
            projects.append(project)
    return projects[:_PROVIDER_MEMORY_WARMUP_MAX_PROJECTS]


def _post_provider_memory_warmup_probe() -> tuple[list[str], list[str]]:
    """Instantiate existing Mem0/Qdrant handles without creating collections or vectors.

    Only already-existing collections are warmed.  This keeps the probe
    projection/read-only: a missing project collection is reported as skipped,
    never created during startup.
    """
    client = get_qdrant_client()
    warmed: list[str] = []
    skipped: list[str] = []
    for project in _provider_memory_warmup_projects():
        collection_name = local_collection_name(project)
        if not client.collection_exists(collection_name):
            skipped.append(project)
            continue
        memory = get_project_mem0_memory(project)
        _ = memory.embedding_model
        warmed.append(project)

    global_name = global_collection_name()
    if client.collection_exists(global_name):
        _ = get_global_core_memory().embedding_model
    return warmed, skipped


async def warmup_provider_probe() -> None:
    if not _PROVIDER_WARMUP_REQUIRED:
        return

    delay = _PROVIDER_WARMUP_INITIAL_DELAY_SECONDS
    attempts = 0
    while True:
        attempts += 1
        _set_provider_warmup_status(attempts=attempts, last_error="", phase="probing")
        try:
            await run_in_threadpool(_post_provider_warmup_probe)
            if _PROVIDER_EMBEDDING_WARMUP_ENABLED:
                embedding_succeeded = False
                embedding_error = ""
                for embedding_attempt in range(1, _PROVIDER_EMBEDDING_WARMUP_ATTEMPTS + 1):
                    _set_provider_warmup_status(
                        embedding_attempts=embedding_attempt,
                        embedding_last_error="",
                        embedding_phase="probing",
                    )
                    try:
                        await run_in_threadpool(_post_provider_embedding_warmup_probe)
                        embedding_succeeded = True
                        break
                    except asyncio.CancelledError:
                        raise
                    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                        embedding_error = str(exc)
                        _set_provider_warmup_status(
                            embedding_ready=False,
                            embedding_last_error=embedding_error,
                            embedding_phase="retrying"
                            if embedding_attempt < _PROVIDER_EMBEDDING_WARMUP_ATTEMPTS
                            else "degraded",
                        )
                        if embedding_attempt < _PROVIDER_EMBEDDING_WARMUP_ATTEMPTS:
                            await asyncio.sleep(_PROVIDER_EMBEDDING_WARMUP_RETRY_DELAY_SECONDS)
                _set_provider_warmup_status(
                    embedding_ready=embedding_succeeded,
                    embedding_last_error="" if embedding_succeeded else embedding_error,
                    embedding_phase="ready" if embedding_succeeded else "degraded",
                )
            if _PROVIDER_MEMORY_WARMUP_ENABLED:
                _set_provider_warmup_status(memory_phase="probing", memory_last_error="")
                try:
                    warmed, skipped = await run_in_threadpool(_post_provider_memory_warmup_probe)
                    _set_provider_warmup_status(
                        memory_ready=True,
                        memory_projects=warmed,
                        memory_skipped_projects=skipped,
                        memory_phase="ready" if warmed else "degraded",
                    )
                except asyncio.CancelledError:
                    raise
                except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError) as exc:
                    _set_provider_warmup_status(
                        memory_ready=False,
                        memory_last_error=str(exc),
                        memory_phase="degraded",
                    )
            _PROVIDER_WARMUP_READY.set()
            _set_provider_warmup_status(ready=True, phase="ready", last_error="")
            return
        except asyncio.CancelledError:
            raise
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            _set_provider_warmup_status(ready=False, phase="retrying", last_error=str(exc))
            await asyncio.sleep(delay)
            delay = min(delay * 2, _PROVIDER_WARMUP_MAX_DELAY_SECONDS)


def _ensure_provider_warmup_ready_sync() -> None:
    if _PROVIDER_WARMUP_READY.is_set():
        return
    if not _PROVIDER_WARMUP_READY.wait(_PROVIDER_READINESS_WAIT_SECONDS):
        raise ResponseTimeout("provider warmup is still in progress")


async def _ensure_provider_warmup_ready() -> None:
    if _PROVIDER_WARMUP_READY.is_set():
        return
    ready = await asyncio.to_thread(_PROVIDER_WARMUP_READY.wait, _PROVIDER_READINESS_WAIT_SECONDS)
    if not ready:
        raise ResponseTimeout("provider warmup is still in progress")


def _is_fallback_grace_error(exc: Exception) -> bool:
    if _configured_fallback_mode() != "explicit":
        return False
    if isinstance(exc, (ResponseTimeout, TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, HTTPException):
        return exc.status_code in {429, 503, 504}
    message = str(exc).lower()
    return any(token in message for token in ("timeout", "timed out", "connection", "semaphore", "backpressure"))


def _configured_fallback_mode() -> str:
    value = os.getenv(_FALLBACK_MODE_ENV, "explicit").strip().lower()
    return value if value in {"explicit", "disabled"} else "disabled"


def _fallback_grace_active() -> bool:
    return time.monotonic() < _FALLBACK_GRACE_ACTIVE_UNTIL


def _mcp_registry_snapshot_path() -> Path:
    configured = os.getenv("BHM_MCP_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured) / "registry.json"
    return settings.runtime_dir / "mcp" / "registry.json"


def _read_json_snapshot(path: Path) -> Any:
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None


def _safe_fallback_provider_warmup() -> dict[str, Any]:
    status = _get_provider_warmup_status()
    return {
        "enabled": bool(status.get("enabled")),
        "ready": bool(status.get("ready")),
        "attempts": int(status.get("attempts") or 0),
        "updated_at": str(status.get("updated_at") or ""),
        "embedding_warmup_enabled": bool(status.get("embedding_warmup_enabled")),
        "embedding_ready": bool(status.get("embedding_ready")),
        "embedding_attempts": int(status.get("embedding_attempts") or 0),
        "embedding_phase": str(status.get("embedding_phase") or ""),
        "memory_warmup_enabled": bool(status.get("memory_warmup_enabled")),
        "memory_ready": bool(status.get("memory_ready")),
        "memory_projects_count": len(status.get("memory_projects") or []),
        "memory_skipped_projects_count": len(status.get("memory_skipped_projects") or []),
        "memory_phase": str(status.get("memory_phase") or ""),
        "error_present": any(
            bool(status.get(key))
            for key in ("last_error", "embedding_last_error", "memory_last_error")
        ),
    }


def _safe_fallback_storage() -> dict[str, Any]:
    state = storage_runtime_state().as_dict()
    return {
        "configured_mode": state.get("configured_mode"),
        "backend": state.get("backend"),
        "readiness": state.get("readiness"),
        "reason": state.get("reason"),
        "database_exists": bool(state.get("database_exists")),
        "database_schema_ready": bool(state.get("database_schema_ready")),
        "parity_confirmed": bool(state.get("parity_confirmed")),
        "writer_offline_confirmed": bool(state.get("writer_offline_confirmed")),
    }


def _fallback_grace_meta(route: str, reason: Exception) -> dict[str, Any]:
    global _FALLBACK_GRACE_ACTIVE_UNTIL
    _FALLBACK_GRACE_ACTIVE_UNTIL = time.monotonic() + (_TELEMETRY_INTERVAL_SECONDS * 2)
    print("[WARN] MCP transport slow, serving data from Fallback Grace disk snapshot", flush=True)
    return {
        "enabled": True,
        "mode": "degraded",
        "policy": _configured_fallback_mode(),
        "read_only": True,
        "route": route,
        "reason": type(reason).__name__,
        "registry": _read_json_snapshot(_mcp_registry_snapshot_path()),
        "provider_warmup": _safe_fallback_provider_warmup(),
        "storage": _safe_fallback_storage(),
    }


def _fallback_memory_records(
    *,
    project: str | None = None,
    memory_type: str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
) -> list[dict]:
    return [
        item for item in _load_live_memories()
        if _memory_matches_filters(
            item,
            project=project,
            memory_type=memory_type,
            concepts=concepts,
            files=files,
            include_archived=include_archived,
            include_logs=include_logs,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
        )
    ]


def _fallback_rank_records(query: str, records: list[dict]) -> list[dict]:
    if not query:
        return sorted(records, key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    scored: list[tuple[float, dict]] = []
    for record in records:
        item = {"memory": record.get("content") or "", "metadata": record.get("metadata") or {}}
        score = _lexical_signal(query, item) + _memory_type_weight(item) + _query_intent_weight(query, item)
        if score > 0:
            scored.append((score, record))
    scored.sort(key=lambda pair: (pair[0], pair[1].get("updated_at") or pair[1].get("created_at") or ""), reverse=True)
    return [record for _, record in scored]


def _fallback_grace_mem0_search(request: SearchRequest, reason: Exception) -> dict:
    records = _fallback_memory_records(
        project=request.project,
        include_archived=request.include_archived,
        include_logs=request.include_logs,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
    )
    records = _fallback_rank_records(request.query, records)[: max(min(request.top_k, 200), 1)]
    results = [
        {
            "id": record.get("source_id"),
            "memory": record.get("content") or "",
            "metadata": {
                **(record.get("metadata") or {}),
                "source_id": record.get("source_id"),
                "project": record.get("project"),
                "memory_type": record.get("memory_type"),
                "tags": record.get("tags") or [],
            },
            "score": 0.0,
        }
        for record in records
    ]
    return {
        "ok": True,
        "degraded": True,
        "read_only": True,
        "result": {"results": results},
        "fallback_grace": _fallback_grace_meta("mem0.search", reason),
        "filters": {
            "project": request.project,
            "domain": request.domain,
            "semantic_type": request.semantic_type,
            "priority": request.priority,
            "include_archived": request.include_archived,
            "include_logs": request.include_logs,
        },
    }


def _fallback_grace_memories_response(
    route: str,
    reason: Exception,
    *,
    project: str | None = None,
    memory_type: str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    query: str = "",
    include_logs: bool = False,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    items = _fallback_memory_records(
        project=project,
        memory_type=memory_type,
        concepts=concepts,
        files=files,
        include_archived=include_archived,
        include_logs=include_logs,
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
    )
    items = _fallback_rank_records(query, items)
    total = len(items)
    start = max(offset, 0)
    capped_limit = max(min(limit, 200), 1)
    window = items[start:start + capped_limit]
    return {
        "memories": [_serialize_memory_record(item) for item in window],
        "total": total,
        "limit": capped_limit,
        "offset": start,
        "degraded": True,
        "read_only": True,
        "fallback_grace": _fallback_grace_meta(route, reason),
    }


def _write_backpressure_headers() -> dict[str, str]:
    return {"Retry-After": str(_WRITE_RETRY_AFTER_SECONDS)}


@asynccontextmanager
async def _bounded_write(operation: str):
    global _WRITE_BACKPRESSURE_ACTIVE, _WRITE_BACKPRESSURE_WAITING

    async with _WRITE_BACKPRESSURE_LOCK:
        queued_or_running = _WRITE_BACKPRESSURE_ACTIVE + _WRITE_BACKPRESSURE_WAITING
        queue_capacity = _MAX_CONCURRENT_WRITES + _WRITE_QUEUE_LIMIT
        if queued_or_running >= queue_capacity:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "write_backpressure",
                    "operation": operation,
                    "active": _WRITE_BACKPRESSURE_ACTIVE,
                    "waiting": _WRITE_BACKPRESSURE_WAITING,
                    "max_concurrent_writes": _MAX_CONCURRENT_WRITES,
                    "queue_limit": _WRITE_QUEUE_LIMIT,
                },
                headers=_write_backpressure_headers(),
            )
        _WRITE_BACKPRESSURE_WAITING += 1

    acquired = False
    try:
        try:
            await asyncio.wait_for(_WRITE_SEMAPHORE.acquire(), timeout=_WRITE_ACQUIRE_TIMEOUT_SECONDS)
            acquired = True
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "write_backpressure_timeout",
                    "operation": operation,
                    "max_concurrent_writes": _MAX_CONCURRENT_WRITES,
                },
                headers=_write_backpressure_headers(),
            ) from None

        async with _WRITE_BACKPRESSURE_LOCK:
            _WRITE_BACKPRESSURE_WAITING -= 1
            _WRITE_BACKPRESSURE_ACTIVE += 1

        try:
            yield
        finally:
            async with _WRITE_BACKPRESSURE_LOCK:
                _WRITE_BACKPRESSURE_ACTIVE -= 1
    finally:
        if not acquired:
            async with _WRITE_BACKPRESSURE_LOCK:
                _WRITE_BACKPRESSURE_WAITING = max(_WRITE_BACKPRESSURE_WAITING - 1, 0)
        else:
            _WRITE_SEMAPHORE.release()


async def _run_bounded_write(operation: str, func, *args, **kwargs):
    async with _bounded_write(operation):
        return await run_in_threadpool(func, *args, **kwargs)


def _hook_queue_path() -> Path:
    return settings.runtime_dir / "live-memory" / "hook-jobs.sqlite3"


def _retention_policy_path() -> Path:
    configured = os.getenv("BHM_RETENTION_POLICY_PATH", "").strip()
    return Path(configured).expanduser() if configured else settings.repo_root / "config" / "retention-policy.json"


def _hook_queue() -> HookJobQueue:
    path = _hook_queue_path().resolve()
    key = str(path)
    with _HOOK_QUEUE_STORES_LOCK:
        queue = _HOOK_QUEUE_STORES.get(key)
        if queue is None:
            queue = HookJobQueue(path, capacity=_HOOK_QUEUE_CAPACITY)
            _HOOK_QUEUE_STORES[key] = queue
        return queue


def _ensure_hook_request_identity(request: BaseModel) -> BaseModel:
    updates: dict[str, Any] = {}
    if not getattr(request, "eventId", None):
        updates["eventId"] = f"obs_bhm_{uuid.uuid4().hex}"
    if not getattr(request, "correlationId", None):
        updates["correlationId"] = str(getattr(request, "sessionId", "") or updates.get("eventId") or "")
    return request.model_copy(update=updates) if updates else request


def _hook_queue_headers() -> dict[str, str]:
    return {"Retry-After": str(_HOOK_QUEUE_RETRY_AFTER_SECONDS)}


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
    interval = max(_HOOK_QUEUE_LEASE_SECONDS / 3.0, 1.0)
    while True:
        await asyncio.sleep(interval)
        renewed = await asyncio.to_thread(
            queue.renew_lease,
            job_id,
            owner=owner,
            lease_seconds=_HOOK_QUEUE_LEASE_SECONDS,
        )
        if not renewed:
            return


async def _hook_queue_worker(*, worker_name: str, kinds: tuple[str, ...], stop_event: asyncio.Event) -> None:
    queue = _hook_queue()
    owner = f"{_HOOK_QUEUE_BOOT_ID}:{worker_name}"
    while True:
        try:
            job = await asyncio.to_thread(
                queue.claim_next,
                kinds=kinds,
                owner=owner,
                lease_seconds=_HOOK_QUEUE_LEASE_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[WARN] Hook queue claim failed for {worker_name}: {exc}", flush=True)
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
            raise
        except Exception as exc:
            redacted_error = redact_secret_text(str(exc)).value[:4000]
            retry_delay = _HOOK_QUEUE_RETRY_BASE_SECONDS * (2 ** max(int(job.get("attempts") or 1) - 1, 0))
            try:
                await asyncio.to_thread(
                    queue.fail,
                    job_id,
                    owner=owner,
                    error=redacted_error,
                    retry_delay_seconds=retry_delay,
                )
            except HookJobLeaseLost:
                print(f"[WARN] Hook job lease lost while recording failure: {job_id}", flush=True)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass


async def _start_hook_queue_workers() -> None:
    global _HOOK_QUEUE_ACCEPTING, _HOOK_QUEUE_STOP_EVENT, _HOOK_QUEUE_TASKS
    if any(not task.done() for task in _HOOK_QUEUE_TASKS):
        _HOOK_QUEUE_ACCEPTING = True
        return
    queue = _hook_queue()
    recovered = await asyncio.to_thread(queue.recover_processing)
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    for index in range(_HOOK_COMPACT_WORKERS):
        tasks.append(
            asyncio.create_task(
                _hook_queue_worker(
                    worker_name=f"compact-{index + 1}",
                    kinds=("compact",),
                    stop_event=stop_event,
                ),
                name=f"bhm-hook-compact-{index + 1}",
            )
        )
    for index in range(_HOOK_IDLE_WORKERS):
        tasks.append(
            asyncio.create_task(
                _hook_queue_worker(
                    worker_name=f"idle-{index + 1}",
                    kinds=("idle",),
                    stop_event=stop_event,
                ),
                name=f"bhm-hook-idle-{index + 1}",
            )
        )
    _HOOK_QUEUE_STOP_EVENT = stop_event
    _HOOK_QUEUE_TASKS = tasks
    _HOOK_QUEUE_ACCEPTING = True
    print(
        f"[INFO] BHM hook queue ready: capacity={queue.capacity} "
        f"compact_workers={_HOOK_COMPACT_WORKERS} idle_workers={_HOOK_IDLE_WORKERS} recovered={recovered}",
        flush=True,
    )


async def _stop_hook_queue_workers() -> None:
    global _HOOK_QUEUE_ACCEPTING, _HOOK_QUEUE_STOP_EVENT, _HOOK_QUEUE_TASKS
    _HOOK_QUEUE_ACCEPTING = False
    stop_event = _HOOK_QUEUE_STOP_EVENT
    tasks = list(_HOOK_QUEUE_TASKS)
    if stop_event is not None:
        stop_event.set()
    if tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=_HOOK_QUEUE_DRAIN_SECONDS,
            )
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            print(
                f"[WARN] Hook queue drain exceeded {_HOOK_QUEUE_DRAIN_SECONDS:.1f}s; "
                "leased jobs will be recovered on restart.",
                flush=True,
            )
    _HOOK_QUEUE_TASKS = []
    _HOOK_QUEUE_STOP_EVENT = None


async def _enqueue_hook_request(kind: str, request: BaseModel) -> tuple[BaseModel, dict[str, Any]]:
    if not _HOOK_QUEUE_ACCEPTING:
        raise HTTPException(
            status_code=503,
            detail={"error": "hook_queue_draining", "kind": kind},
            headers=_hook_queue_headers(),
        )
    durable_request = _ensure_hook_request_identity(request)
    priority = 10 if kind == "compact" else 100
    try:
        result = await asyncio.to_thread(
            _hook_queue().enqueue,
            kind,
            durable_request.model_dump(mode="json"),
            priority=priority,
            max_attempts=_HOOK_QUEUE_MAX_ATTEMPTS,
        )
    except HookQueueFull as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "hook_queue_full",
                "kind": kind,
                "pending": exc.pending,
                "capacity": exc.capacity,
            },
            headers=_hook_queue_headers(),
        ) from exc
    except HookJobCollision as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "hook_job_event_id_collision", "eventId": exc.event_id},
        ) from exc
    except HookQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "hook_queue_unavailable", "detail": str(exc)},
            headers=_hook_queue_headers(),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "hook_queue_unavailable",
                "detail": redact_secret_text(str(exc)).value[:500],
            },
            headers=_hook_queue_headers(),
        ) from exc

    return durable_request, {
        "success": True,
        "accepted": True,
        "action": "queued" if result.inserted else f"already_{result.status}",
        "durability": "sqlite-wal",
        "job": {
            "id": result.job_id,
            "status": result.status,
            "inserted": result.inserted,
            "pending": result.pending,
            "capacity": result.capacity,
            "createdAt": result.created_at,
        },
        "observation": {
            "eventId": result.event_id,
            "state": "pending" if result.status in {"queued", "processing"} else result.status,
        },
        "hook": {
            "type": str(getattr(durable_request, "hookType", "")),
            "sessionId": str(getattr(durable_request, "sessionId", "")),
            "project": str(getattr(durable_request, "project", "")),
        },
    }


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
    try:
        with urllib.request.urlopen(f"{settings.qdrant_url.rstrip('/')}/healthz", timeout=_QDRANT_HEALTH_TIMEOUT_SECONDS) as response:
            return 200 <= int(response.status) < 300
    except (OSError, TimeoutError, urllib.error.URLError):
        return False


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


def _crystals_total_sync() -> int:
    total = 0
    for record in _load_live_memories():
        metadata = record.get("metadata") or {}
        memory_type = str(record.get("memory_type") or metadata.get("memory_type") or "").lower()
        concepts = {str(item).lower() for item in (record.get("tags") or [])}
        if memory_type in {"crystal", "fact-crystal"} or "fact-crystal" in concepts:
            total += 1
        elif metadata.get("crystallized_from"):
            total += 1
    return total


def _architectural_laws_total_sync() -> int:
    total = 0
    for record in _load_live_memories():
        metadata = record.get("metadata") or {}
        memory_type = str(record.get("memory_type") or metadata.get("memory_type") or "").lower()
        concepts = {str(item).lower() for item in (record.get("tags") or [])}
        semantic_type = str(metadata.get("semantic_type") or "").lower()
        if memory_type in {"architecture", "adr"}:
            total += 1
        elif semantic_type == "decision-log" and {"architecture", "law", "adr"} & concepts:
            total += 1
    return total


def _collect_host_telemetry_sync() -> dict[str, Any]:
    queue_status = (
        _hook_queue().status()
        if _hook_queue_path().exists()
        else {
            "pending": 0,
            "capacity": _HOOK_QUEUE_CAPACITY,
            "counts": {"queued": 0, "processing": 0, "completed": 0, "failed": 0},
            "oldestQueuedAgeMs": 0,
        }
    )
    return {
        "bhm_working_set_mb": _current_working_set_mb(),
        "wsl_shared_overhead_gb": _wsl_shared_overhead_gb(),
        "qdrant_healthy": _qdrant_healthy_sync(),
        "crystals_total": _crystals_total_sync(),
        "architectural_laws_total": _architectural_laws_total_sync(),
        "hook_queue_pending": queue_status["pending"],
        "hook_queue_capacity": queue_status["capacity"],
        "hook_queue_queued": queue_status["counts"]["queued"],
        "hook_queue_processing": queue_status["counts"]["processing"],
        "hook_queue_failed": queue_status["counts"]["failed"],
        "hook_queue_oldest_age_ms": queue_status["oldestQueuedAgeMs"],
    }


async def _collect_sys_status_payload() -> dict[str, Any]:
    host = await asyncio.to_thread(_collect_host_telemetry_sync)
    warmup = _get_provider_warmup_status()
    return {
        "event": "sys_status",
        "data": {
            "mcp_active_pipes": 0,
            "mcp_max_instances": 0,
            "mcp_surface": resolve_mcp_surface().value,
            "mcp_max_frame_bytes": 0,
            "mcp_client_timeout_seconds": 0.0,
            "mcp_dispatch_timeout_seconds": 0.0,
            "mcp_transport": _MCP_STREAMABLE_HTTP.contract_snapshot()["sessions"],
            "storage": storage_runtime_state().as_dict(),
            "memory_store": _memory_store_state().as_dict(),
            "fallback_mode": _configured_fallback_mode(),
            "backpressure_semaphore_value": getattr(_WRITE_SEMAPHORE, "_value", 0),
            "backpressure_max": _MAX_CONCURRENT_WRITES,
            "fallback_grace_active": _fallback_grace_active(),
            "launcher_circuit_breaker_status": "STREAMABLE_HTTP",
            "llm_warmup_status": "READY" if warmup.get("ready") else str(warmup.get("phase") or "WARMING").upper(),
            **host,
        },
    }


async def _telemetry_harvester_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_TELEMETRY_INTERVAL_SECONDS)
            if _MEMORY_PULSE_BUS.client_count == 0:
                continue
            await _MEMORY_PULSE_BUS.broadcast(await _collect_sys_status_payload())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[WARN] BHM telemetry harvester skipped tick: {exc}", flush=True)


def _mcp_model_dump(value: Any) -> Any:
    if hasattr(value, "to_mcp_tool"):
        value = value.to_mcp_tool()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: _mcp_model_dump(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mcp_model_dump(item) for item in value]
    return value


def _jsonrpc_success(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _validate_bhm_remember_mcp_arguments(arguments: dict[str, Any]) -> str | None:
    argument_keys = set(arguments)
    unknown_keys = argument_keys - _BHM_REMEMBER_ALLOWED_ARGUMENTS
    if unknown_keys:
        names = ", ".join(sorted(unknown_keys))
        return f"Unsupported bhm_remember argument(s): {names}"
    if "concepts" in arguments and not isinstance(arguments["concepts"], list):
        return "bhm_remember concepts must be an array"
    if "files" in arguments and not isinstance(arguments["files"], list):
        return "bhm_remember files must be an array"
    if "metadata" in arguments and arguments["metadata"] is not None and not isinstance(arguments["metadata"], dict):
        return "bhm_remember metadata must be an object"
    return None


async def _handle_mcp_gateway_jsonrpc_core(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if not isinstance(params, dict):
        return _jsonrpc_error(request_id, -32600, "JSON-RPC params must be an object")
    if method in {"notifications/initialized", "notifications/cancelled", "exit"}:
        if is_notification:
            return None
        return _jsonrpc_error(request_id, -32600, f"{method} must be sent as a notification")
    if is_notification:
        return None
    if method == "initialize":
        try:
            requested_version = negotiate_protocol_version(params.get("protocolVersion"))
        except ProtocolContractError as exc:
            return _jsonrpc_error(request_id, exc.code, str(exc))
        surface = resolve_mcp_surface()
        return _jsonrpc_success(
            request_id,
            {
                "protocolVersion": requested_version,
                "capabilities": initialize_capabilities(),
                "serverInfo": {
                    "name": "bhm",
                    "version": BROKER_VERSION,
                    "surface": surface.value,
                    "adminCapabilityRequired": surface.value == "admin",
                    "adminCapabilityConfigured": bool(configured_admin_capability()),
                },
            },
        )
    if method == "shutdown":
        return _jsonrpc_success(request_id, {})
    if method == "ping":
        return _jsonrpc_success(request_id, {})
    if method in {"resources/list", "resources/templates/list", "prompts/list"}:
        key = {
            "resources/list": "resources",
            "resources/templates/list": "resourceTemplates",
            "prompts/list": "prompts",
        }[method]
        return _jsonrpc_success(request_id, {key: []})
    if method == "tools/list":
        from . import bhm_mcp

        surface = resolve_mcp_surface()
        requested_tools = surface
        if surface.value == "admin" and not is_admin_capability_valid(extract_mcp_capability(params)):
            requested_tools = resolve_mcp_surface("core")
        tools = filter_tools(await bhm_mcp.mcp.list_tools(), requested_tools)
        return _jsonrpc_success(request_id, {"tools": [_mcp_model_dump(tool) for tool in tools]})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not name:
            return _jsonrpc_error(request_id, -32600, "tools/call requires params.name")
        if not isinstance(arguments, dict):
            return _jsonrpc_error(request_id, -32600, "tools/call params.arguments must be an object")
        surface = resolve_mcp_surface()
        if not is_tool_allowed(name, surface):
            return _jsonrpc_error(
                request_id,
                -32601,
                f"MCP tool '{name}' is not available on '{surface.value}' surface",
            )
        if requires_admin_capability(name, surface) and not is_admin_capability_valid(extract_mcp_capability(params)):
            return _jsonrpc_error(request_id, -32003, "BHM admin capability is required for this tool")
        if name == "bhm_remember":
            validation_error = _validate_bhm_remember_mcp_arguments(arguments)
            if validation_error:
                return _jsonrpc_error(request_id, -32600, validation_error)
        try:
            from . import bhm_mcp

            content = await bhm_mcp.mcp.call_tool(name, arguments)
        except Exception as exc:
            return _jsonrpc_error(request_id, -32603, f"{name} failed: {exc}")
        dumped = _mcp_model_dump(content)
        if isinstance(dumped, dict):
            result: dict[str, Any] = {
                "content": dumped.get("content") or [],
                "isError": bool(dumped.get("isError", dumped.get("is_error", False))),
            }
            structured_content = dumped.get("structuredContent", dumped.get("structured_content"))
            if structured_content is not None:
                result["structuredContent"] = structured_content
            meta = dumped.get("_meta", dumped.get("meta"))
            if meta is not None:
                result["_meta"] = meta
        else:
            result = {"content": dumped if isinstance(dumped, list) else [], "isError": False}
        return _jsonrpc_success(request_id, result)
    return _jsonrpc_error(request_id, -32601, f"Unsupported MCP method: {method}")


def _mcp_usage_operation(message: dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return "invalid"
    method = normalize_operation(message.get("method"), fallback="unknown")
    if method != "tools/call":
        return method
    params = message.get("params")
    if not isinstance(params, dict):
        return method
    tool_name = normalize_operation(params.get("name"), fallback="other")
    return normalize_operation(f"{method}:{tool_name}", fallback=method)


def _mcp_usage_outcome(response: dict[str, Any] | None) -> tuple[str, bool]:
    if response is None:
        return "notification", False
    error = response.get("error")
    if not isinstance(error, dict):
        return "success", False
    try:
        error_code = int(error.get("code"))
    except (TypeError, ValueError):
        error_code = 0
    if error_code == -32004:
        return "timeout", True
    return "error", False


async def _handle_mcp_gateway_jsonrpc_async(message: dict[str, Any]) -> dict[str, Any] | None:
    started_at = time.perf_counter()
    response: dict[str, Any] | None = None
    status = "success"
    timed_out = False
    try:
        response = await _handle_mcp_gateway_jsonrpc_core(message)
        status, timed_out = _mcp_usage_outcome(response)
        return response
    except (asyncio.TimeoutError, TimeoutError, ResponseTimeout):
        status = "timeout"
        timed_out = True
        raise
    except Exception:
        status = "exception"
        raise
    finally:
        _USAGE_TELEMETRY.record(
            surface="mcp",
            operation=_mcp_usage_operation(message),
            status=status,
            duration_ms=monotonic_elapsed_ms(started_at),
            timeout=timed_out,
        )


_MCP_STREAMABLE_HTTP = BhmStreamableHttpGateway(
    _handle_mcp_gateway_jsonrpc_async,
    server_version=BROKER_VERSION,
)
_UI_SESSIONS = UiSessionRegistry()
MAX_UI_EXCHANGE_BODY_BYTES = 16 * 1024


class UiSessionExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_token: StrictStr = Field(min_length=32, max_length=256)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _request_host_parts(host_header: str | None) -> tuple[str, str] | None:
    raw_host = str(host_header or "").strip()
    if not raw_host:
        return None
    try:
        parsed = urlsplit(f"http://{raw_host}")
        hostname = str(parsed.hostname or "").casefold()
    except ValueError:
        return None
    if hostname not in _LOOPBACK_HOSTS:
        return None
    return hostname, raw_host.casefold()


def _ui_browser_request_is_same_origin(request: Request, *, require_origin: bool = False) -> bool:
    host_parts = _request_host_parts(request.headers.get("host"))
    if host_parts is None:
        return False
    fetch_site = str(request.headers.get("sec-fetch-site") or "").strip().casefold()
    if fetch_site != "same-origin":
        return False
    origin_value = str(request.headers.get("origin") or "").strip()
    if not origin_value:
        return not require_origin
    try:
        origin = urlsplit(origin_value)
        origin_host = str(origin.hostname or "").casefold()
    except ValueError:
        return False
    return (
        origin.scheme.casefold() in {"http", "https"}
        and origin_host in _LOOPBACK_HOSTS
        and origin.netloc.casefold() == host_parts[1]
    )


def _ui_request_is_loopback(request: Request) -> bool:
    """Allow direct browser bootstrap only from the local loopback listener."""

    client_host = str(getattr(request.client, "host", "") or "").strip()
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return client_host.casefold() == "localhost"


def _websocket_origin_is_allowed(websocket: WebSocket, *, require_exact_origin: bool) -> bool:
    host_parts = _request_host_parts(websocket.headers.get("host"))
    if host_parts is None:
        return False
    origin_value = str(websocket.headers.get("origin") or "").strip()
    if not origin_value:
        return not require_exact_origin
    try:
        origin = urlsplit(origin_value)
        origin_host = str(origin.hostname or "").casefold()
    except ValueError:
        return False
    if origin.scheme.casefold() not in {"http", "https"} or origin_host not in _LOOPBACK_HOSTS:
        return False
    return not require_exact_origin or origin.netloc.casefold() == host_parts[1]


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    _UI_SESSIONS.reset()
    caller_configuration_error = caller_auth_configuration_error()
    if caller_configuration_error:
        raise RuntimeError(
            f"BHM caller authentication is not configured: {caller_configuration_error}"
        )
    memory_store = _memory_store_state()
    if memory_store.configured_mode == MemoryStoreMode.SQLITE_AUTHORITATIVE.value and not memory_store.ready:
        raise RuntimeError(
            "sqlite-authoritative memory mode is not ready: "
            f"{memory_store.reason}"
        )
    await _wait_for_required_storage_ready()
    collection_report = await asyncio.to_thread(ensure_memory_collections, settings.qdrant_collection)
    print(
        "[INFO] BHM Qdrant contours ready: "
        f"local={collection_report['local']['collection_name']} "
        f"global={collection_report['global']['collection_name']}",
        flush=True,
    )
    warmup_task = asyncio.create_task(warmup_provider_probe())
    boot_report_task: asyncio.Task[None] | None = None
    if _boot_report_is_pending():
        boot_report_task = asyncio.create_task(_finalize_pending_boot_report(warmup_task))
    telemetry_task = asyncio.create_task(_telemetry_harvester_loop())
    await _start_hook_queue_workers()
    try:
        async with _MCP_STREAMABLE_HTTP.run():
            yield
    finally:
        _UI_SESSIONS.reset()
        await _stop_hook_queue_workers()
        if boot_report_task is not None:
            boot_report_task.cancel()
            try:
                await boot_report_task
            except asyncio.CancelledError:
                pass
        telemetry_task.cancel()
        try:
            await telemetry_task
        except asyncio.CancelledError:
            pass
        warmup_task.cancel()
        try:
            await warmup_task
        except asyncio.CancelledError:
            pass
        await _cleanup_registered_infra_processes(reason="api_shutdown")


app = FastAPI(title=settings.app_name, redoc_url=None, lifespan=_app_lifespan)
app.router.routes.append(
    Route(
        "/mcp",
        endpoint=_MCP_STREAMABLE_HTTP.asgi_app,
        methods=["GET", "POST", "DELETE"],
        name="bhm-streamable-http-mcp",
    )
)
graph = build_graph()
_PROJECT_REGISTRY = get_default_project_registry()
STATIC_DIR = Path(__file__).parent / "static"
_OBSERVATION_STORE_LOCK = threading.RLock()
_OBSERVATION_SQLITE_STORES: dict[str, ObservationStore] = {}
_MEMORY_SERVICE_LOCK = threading.RLock()
_MEMORY_SERVICES: dict[str, SQLiteMemoryService] = {}
_TASK_LIFECYCLE_LOCK = threading.RLock()
_JSON_STORE_LOCKS: dict[str, threading.RLock] = {}
_JSON_STORE_LOCKS_LOCK = threading.RLock()
_JSON_REPLACE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4, 0.8)


@app.exception_handler(StorageNotReady)
async def storage_not_ready_handler(_request: Request, exc: StorageNotReady) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": {"code": "storage_not_ready", "reason": str(exc)}},
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
        principal = configured_principal if is_caller_token_valid(supplied) else None
        auth_kind = "caller_bearer" if principal is not None else ""
        if principal is None and ui_session_route_allowed(request.url.path, request.method):
            session_candidate = request.cookies.get(UI_SESSION_COOKIE)
            session_principal = _UI_SESSIONS.resolve_session(session_candidate)
            if session_principal is not None and _ui_browser_request_is_same_origin(
                request,
                require_origin=request.method.upper() not in {"GET", "HEAD"},
            ):
                principal = session_principal
                auth_kind = "ui_session"
        if principal is None:
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                content={"detail": {"code": "caller_auth_required"}},
            )

        project_sources: list[Any] = [request.query_params]
        if (
            policy is CallerRoutePolicy.PROJECT
            and not principal.all_projects
            and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
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
            project_error = authorize_projects(
                principal,
                extract_request_projects(*project_sources),
                require_explicit=not principal.all_projects,
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


def _request_surface(request: Request) -> str:
    caller_surface = str(request.headers.get("x-bhm-caller-surface") or "").strip().lower()
    return "mcp" if caller_surface == "mcp" else "rest"


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
        _USAGE_TELEMETRY.record(
            surface=_request_surface(request),
            operation=_rest_usage_operation(request),
            status=status,
            duration_ms=monotonic_elapsed_ms(started_at),
            response_size_bytes=_response_size_from_headers(response),
            timeout=timed_out,
        )


def _json_store_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _JSON_STORE_LOCKS_LOCK:
        lock = _JSON_STORE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _JSON_STORE_LOCKS[key] = lock
        return lock


class MemoryPulseBus:
    def __init__(self) -> None:
        self._clients: dict[WebSocket, frozenset[str] | None] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()

    async def connect(self, websocket: WebSocket, projects: frozenset[str] | None = None) -> None:
        await websocket.accept()
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._clients[websocket] = projects

    def disconnect(self, websocket: WebSocket) -> None:
        with self._lock:
            self._clients.pop(websocket, None)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        with self._lock:
            clients = list(self._clients.items())
        is_pulse = str(payload.get("event") or "") == "pulse"
        pulse_project = ""
        if is_pulse:
            pulse_project = _canonical_project(str(payload.get("project") or "")) if payload.get("project") else ""
        disconnected: list[WebSocket] = []
        for client, subscribed_projects in clients:
            if is_pulse and subscribed_projects is not None and (
                not pulse_project or pulse_project not in subscribed_projects
            ):
                continue
            try:
                await client.send_json(payload)
            except (RuntimeError, WebSocketDisconnect):
                disconnected.append(client)
        if disconnected:
            with self._lock:
                for client in disconnected:
                    self._clients.pop(client, None)

    def emit_pulse(self, node_id: str, project: str | None = None) -> None:
        if not node_id:
            return
        with self._lock:
            loop = self._loop
            has_clients = bool(self._clients)
        if loop is None or loop.is_closed() or not has_clients:
            return
        payload = {"event": "pulse", "node_id": node_id, "project": _canonical_project(project) if project else ""}
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            running_loop.create_task(self.broadcast(payload))
        else:
            asyncio.run_coroutine_threadsafe(self.broadcast(payload), loop)


_MEMORY_PULSE_BUS = MemoryPulseBus()


def _emit_memory_pulse(node_id: Any, project: str | None = None) -> None:
    _MEMORY_PULSE_BUS.emit_pulse(str(node_id or ""), project)


def _emit_memory_pulses_from_records(records: list[dict]) -> None:
    seen: set[str] = set()
    for record in records:
        node_id = str(record.get("source_id") or record.get("id") or "")
        if node_id and node_id not in seen:
            seen.add(node_id)
            _emit_memory_pulse(node_id, str(record.get("project") or ""))


def _emit_memory_pulses_from_mem0_items(items: list[dict]) -> None:
    seen: set[str] = set()
    for item in items:
        metadata = item.get("metadata") or {}
        node_id = str(metadata.get("source_id") or item.get("source_id") or item.get("id") or "")
        if node_id and node_id not in seen:
            seen.add(node_id)
            _emit_memory_pulse(node_id, str(metadata.get("project") or item.get("project") or ""))


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


FactCrystalDomain = Literal["frontend", "backend", "infra", "security", "product", "general"]
FactCrystalPriority = Literal["low", "medium", "high", "critical"]
FactCrystalSemanticType = Literal["architecture", "bugfix", "feature", "refactor", "knowledge"]


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
    version: str | None = Field(default=None, description='Taxonomy version, for example "1.0".')
    importance_score: int | None = Field(default=None, ge=1, le=10, description="Cognitive importance from 1 to 10.")


class SearchRequest(BaseModel):
    query: str
    project: str | None = None
    user_id: str = settings.mem0_user_id
    # Keep federated/compatibility retrieval bounded; the fallback path already
    # caps at 200, so accepting larger values only creates timeout-prone work.
    top_k: int = Field(default=5, ge=1, le=200)
    domain: str | None = None
    semantic_type: str | None = None
    priority: str | None = None
    include_archived: bool = False
    include_logs: bool = False


class RememberRequest(BaseModel):
    project: str = "e-github-workspace"
    type: str = "workflow"
    content: str
    concepts: list[str] | None = None
    files: list[str] | None = None
    upsert_key: str | None = None
    metadata: MemoryMetadata | None = None


class MemoryUpdateRequest(BaseModel):
    id: str
    project: str | None = None
    type: str | None = None
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
    concepts: list[str] | None = None
    files: list[str] | None = None
    include_archived: bool = False
    include_logs: bool = False
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
    concepts: list[str] | None = None
    files: list[str] | None = None
    include_archived: bool = False
    include_logs: bool = False
    domain: str | None = None
    semantic_type: str | None = None
    priority: str | None = None
    profile: str | None = None
    limit: int | None = Field(default=None, ge=1, le=50)
    token_budget: int | None = Field(default=None, ge=64, le=MAX_CONTEXT_TOKEN_BUDGET)


class RetrievalExplainRequest(BaseModel):
    """Request for bounded ranking and routing diagnostics."""

    query: str
    project: str | None = None
    memory_type: str | None = None
    concepts: list[str] | None = None
    files: list[str] | None = None
    include_archived: bool = False
    include_logs: bool = False
    domain: str | None = None
    semantic_type: str | None = None
    priority: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class MemoryUsedRequest(BaseModel):
    """Explicit access-feedback signal for already retrieved memory ids."""

    ids: list[str] = Field(min_length=1, max_length=50)
    project: str | None = None
    reason: str = Field(default="", max_length=200)


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


class MemoryUpsertRequest(BaseModel):
    upsert_key: str
    project: str
    type: str = "workflow"
    content: str
    concepts: list[str] | None = None
    files: list[str] | None = None
    metadata: MemoryMetadata | None = None


class MemoryLinkRequest(BaseModel):
    source_id: str
    target_id: str
    relation: str
    project: str
    metadata: MemoryMetadata | None = None


class MemoryLinkDeleteRequest(BaseModel):
    source_id: str
    target_id: str
    relation: str
    project: str


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
    root: StrictStr = Field(default=".", min_length=1, max_length=512)
    apply: StrictBool = False
    build_graph: StrictBool = True
    force_refresh: StrictBool = False
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
    project: str
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
    project: str
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
    include_archived: bool = False
    limit: int = 20


class HardDeleteMemoryRequest(BaseModel):
    id: str
    project: str | None = None


class BatchUpsertMemoriesRequest(BaseModel):
    items: list[MemoryUpsertRequest]


class BatchLinkMemoriesRequest(BaseModel):
    items: list[MemoryLinkRequest]


class BatchAttachSourceRefsRequest(BaseModel):
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
    items: list[dict]


class RepairLiveIndexesRequest(BaseModel):
    remove_orphan_links: bool = True
    remove_orphan_artifacts: bool = False


class RebuildProjectSummaryRequest(BaseModel):
    project: str
    upsert_key: str | None = None


class ProjectSummaryListRequest(BaseModel):
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
    projects: list[str] | None = None


class RelationApplySuggestionsRequest(BaseModel):
    project: str | None = None
    min_score: float = 0.65
    limit: int = 20
    include_relates_to: bool = False


class MemoryMergePreviewRequest(BaseModel):
    project: str
    source_id: str
    target_id: str


class SchemaUpgradeAllRequest(BaseModel):
    project: str | None = None


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


class ProjectSummaryCompareRequest(BaseModel):
    left_project: str
    right_project: str


class RecentFailuresFeedRequest(BaseModel):
    project: str | None = None
    limit: int = 20


class ProjectOnlyRequest(BaseModel):
    project: str | None = None


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


class AdminImportApplyRequest(BaseModel):
    path: str
    merge_mode: str = "upsert"


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
    project: str
    limit: int = 20
    archive_sources: bool = True


class TypeMigrateRequest(BaseModel):
    id: str
    project: str | None = None
    new_type: str


class HybridSearchRequest(BaseModel):
    query: str
    project: str | None = None
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
    content: str = ""
    sizeLimit: int = 2000
    description: str = ""
    pinned: bool = True
    scope: str = "project"
    project: str | None = None


class SlotAppendRequest(BaseModel):
    label: str
    text: str
    project: str | None = None


class SlotReplaceRequest(BaseModel):
    label: str
    content: str
    project: str | None = None


class SlotLabelRequest(BaseModel):
    label: str
    project: str | None = None


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


def _secure_observation_request_model(request: BaseModel, *, max_input_bytes: int) -> BaseModel:
    try:
        secured_payload = secure_observation_payload(
            request.model_dump(mode="json"),
            max_input_bytes=max_input_bytes,
        )
    except ObservationPayloadTooLarge as exc:
        raise HTTPException(status_code=413, detail=exc.as_detail()) from exc
    return request.__class__.model_validate(secured_payload)


def _memory_store_state():
    return memory_runtime_storage_state(settings.runtime_dir, switch_wired=True)


def _memory_store_is_authoritative() -> bool:
    return _memory_store_state().configured_mode == MemoryStoreMode.SQLITE_AUTHORITATIVE.value


def _memory_service() -> SQLiteMemoryService:
    config = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir)
    key = str(config.database_path)
    with _MEMORY_SERVICE_LOCK:
        service = _MEMORY_SERVICES.get(key)
        if service is None:
            service = SQLiteMemoryService(config.database_path)
            _MEMORY_SERVICES[key] = service
        return service


def _slot_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "slots.json"


def _lesson_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "lessons.json"


def _observe_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "observations.sqlite3"


def _observation_store() -> ObservationStore:
    path = _observe_store_path().resolve()
    key = str(path)
    with _OBSERVATION_STORE_LOCK:
        store = _OBSERVATION_SQLITE_STORES.get(key)
        if store is None:
            store = ObservationStore(path)
            _OBSERVATION_SQLITE_STORES[key] = store
        return store


def _memory_link_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "memory-links.json"


def _checkpoint_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "checkpoints.json"


def _project_map_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "project-maps.json"


def _adr_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "adrs.json"


def _handoff_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "handoffs.json"


def _session_record_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "session-records.json"


def _task_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "tasks.json"


def _task_context_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "task-contexts.json"


def _risk_register_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "risk-registers.json"


def _validation_snapshot_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "validation-snapshots.json"


def _entity_catalog_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "entity-catalogs.json"


def _policy_profile_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "policy-profile.json"


def _load_live_memories() -> list[dict]:
    try:
        return _memory_service().load_records()
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc


def _semantic_projected_code_metadata_count(
    *,
    project: str,
    graph_snapshot_id: str,
    graph_digest: str,
) -> int:
    """Count active SQLite projection candidates bound to one graph epoch.

    This is intentionally authoritative/local only.  It does not instantiate
    Mem0, query an embedding provider, contact Qdrant, or return source text.
    The outbox state below separately proves whether these rows have drained to
    the projection layer.
    """

    accepted_projects = _project_aliases(project)
    prefix = f"code-metadata:{_canonical_project(project)}:"
    service = _memory_service()
    counter = getattr(service, "count_projected_code_metadata", None)
    if callable(counter):
        return int(
            counter(
                projects=accepted_projects,
                upsert_key_prefix=prefix,
                graph_snapshot_id=graph_snapshot_id,
                graph_digest=graph_digest,
            )
        )

    # Compatibility fallback for narrow test doubles and older disposable
    # runtimes.  The live service always takes the bounded SQL path above.
    count = 0
    for record in _load_live_memories():
        if accepted_projects and record.get("project") not in accepted_projects:
            continue
        if _memory_lifecycle(record) != "active":
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        if str(metadata.get("source_kind") or "") != "code-graph-metadata":
            continue
        if not str(metadata.get("upsert_key") or "").startswith(prefix):
            continue
        if str(metadata.get("graph_snapshot_id") or "") != str(graph_snapshot_id or ""):
            continue
        if str(metadata.get("graph_digest") or "") != str(graph_digest or ""):
            continue
        count += 1
    return count


def _fast_semantic_runtime_slo_status() -> str:
    """Return the semantic-request SLO state without re-entering health routes.

    ``bhm/health/slo`` intentionally performs a full readiness/cutover probe,
    which is appropriate for an operator endpoint but far too expensive to
    call again from an already authenticated code-search request.  The
    readiness gate has already verified the authoritative store and outbox;
    this fallback preserves the same fail-closed projection/provider boundary
    when the gate is disabled.
    """

    try:
        if not _memory_store_is_authoritative():
            return "breached"
        outbox = _memory_service().outbox_status()
        pending = max(int(outbox.get("pending") or 0), 0) + max(int(outbox.get("processing") or 0), 0)
        failed = max(int(outbox.get("failed") or 0), 0) + max(int(outbox.get("dead_letter") or 0), 0)
        if pending or failed:
            return "breached"
        if not bool(_get_provider_warmup_status().get("ready")):
            return "breached"
        return "healthy"
    except (MemoryServiceNotReady, OSError, sqlite3.Error, ValueError, TypeError):
        return "breached"


async def _semantic_readiness_receipt(
    *,
    project: str,
    current_graph: Mapping[str, Any],
    repository_snapshot: Mapping[str, Any],
    embedding_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Build/read the bounded per-project readiness receipt."""

    graph_snapshot_id = str(current_graph.get("graph_snapshot_id") or "")
    graph_digest = str(current_graph.get("graph_digest") or "")
    repository_snapshot_id = str(repository_snapshot.get("snapshot_id") or "")
    graph_repository_snapshot_id = str(current_graph.get("repository_snapshot_id") or "")
    repository_digest = str(repository_snapshot.get("snapshot_digest") or "")
    source_count = len(list(repository_snapshot.get("files") or []))
    max_files = _env_int("BHM_SEMANTIC_READINESS_MAX_FILES", 128, 1)
    selected_count = min(source_count, max_files)
    projected_count = await asyncio.to_thread(
        _semantic_projected_code_metadata_count,
        project=project,
        graph_snapshot_id=graph_snapshot_id,
        graph_digest=graph_digest,
    )
    try:
        outbox = _memory_service().outbox_status()
    except MemoryServiceNotReady:
        outbox = {"pending": 1, "failed": 1}
    projection_pending = max(int(outbox.get("pending") or 0), 0)
    projection_failed = max(int(outbox.get("failed") or 0), 0)
    warmup_status = _get_provider_warmup_status()
    provider_ready = bool(warmup_status.get("ready"))
    project_warmup_enabled, project_warmup_ready, project_warmup_phase = project_warmup_state(
        project, warmup_status
    )
    runtime_slo_status = (
        "healthy"
        if _memory_store_is_authoritative() and projection_pending == 0 and projection_failed == 0
        else "breached"
    )
    embedding_contract_digest = hashlib.sha256(
        json.dumps(dict(embedding_contract), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    key = build_readiness_key(
        project=project,
        graph_snapshot_id=graph_snapshot_id,
        graph_digest=graph_digest,
        repository_snapshot_digest=repository_digest,
        parser_registry_digest=PARSER_REGISTRY_DIGEST,
        embedding_contract_digest=embedding_contract_digest,
        source_row_count=source_count,
        selected_count=selected_count,
        projected_count=projected_count,
        projection_pending=projection_pending,
        projection_failed=projection_failed,
        provider_ready=provider_ready,
        project_warmup_enabled=project_warmup_enabled,
        project_warmup_ready=project_warmup_ready,
    )
    cached = _SEMANTIC_READINESS_CACHE.get(key)
    if cached is not None:
        return cached
    receipt = evaluate_semantic_readiness(
        project=project,
        graph_snapshot_id=graph_snapshot_id,
        graph_digest=graph_digest,
        current_graph_snapshot_id=graph_snapshot_id,
        graph_repository_snapshot_id=graph_repository_snapshot_id,
        current_repository_snapshot_id=repository_snapshot_id,
        repository_snapshot_digest=repository_digest,
        parser_registry_digest=PARSER_REGISTRY_DIGEST,
        embedding_contract_digest=embedding_contract_digest,
        provider_ready=provider_ready,
        runtime_slo_status=runtime_slo_status,
        source_row_count=source_count,
        selected_count=selected_count,
        projected_count=projected_count,
        projection_pending=projection_pending,
        projection_failed=projection_failed,
        skipped_count=max(source_count - selected_count, 0),
        project_warmup_enabled=project_warmup_enabled,
        project_warmup_ready=project_warmup_ready,
    )
    receipt["cache_key"] = key
    receipt["repository_snapshot_id"] = repository_snapshot_id
    receipt["provider"]["project_warmup_phase"] = project_warmup_phase
    return _SEMANTIC_READINESS_CACHE.put(key, receipt)


def _save_live_memories(items: list[dict]) -> Path:
    try:
        service = _memory_service()
        current = {
            str(record.get("source_id")): record
            for record in service.load_records()
            if record.get("source_id")
        }
        changed: list[dict] = []
        for record in items:
            source_id = str(record.get("source_id") or "").strip()
            if not source_id:
                raise MemoryServiceValidationError("SQLite memory record missing source_id")
            if current.get(source_id) != record:
                changed.append(record)
        return service.upsert_records(changed)
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc


def _project_aliases(project: str | None) -> set[str]:
    if not project:
        return set()
    return _PROJECT_REGISTRY.accepted_values(project)


def _canonical_project(project: str | None) -> str:
    return _PROJECT_REGISTRY.canonicalize(project)


def _find_live_memory(memory_id: str, project: str | None = None) -> dict | None:
    accepted_projects = _project_aliases(project)
    for item in _load_live_memories():
        if item.get("source_id") != memory_id:
            continue
        if accepted_projects and item.get("project") not in accepted_projects:
            continue
        _emit_memory_pulse(item.get("source_id"), str(item.get("project") or ""))
        return item
    return None


def _replace_live_memory(record: dict) -> None:
    live_records = _load_live_memories()
    for index, item in enumerate(live_records):
        if item.get("source_id") != record.get("source_id"):
            continue
        live_records[index] = record
        _save_live_memories(live_records)
        return
    raise HTTPException(status_code=404, detail="memory not found in live store")


def _append_memory_changelog(record: dict, action: str, details: dict | None = None) -> dict:
    metadata = record.setdefault("metadata", {})
    changelog = metadata.setdefault("changelog", [])
    changelog.append(
        {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "action": action,
            "details": details or {},
        }
    )
    metadata["changelog"] = changelog[-200:]
    return record


def _delete_live_memory(request: MemoryDeleteRequest) -> dict:
    try:
        existing = _memory_service().get_record(request.id)
        accepted_projects = _project_aliases(request.project)
        if existing is None or (
            request.project and existing.get("project") not in accepted_projects
        ):
            raise HTTPException(status_code=404, detail="memory not found in SQLite store")
        deleted = _memory_service().tombstone(
            request.id,
            reason="user_delete",
        )
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc
    if deleted is None:
        raise HTTPException(status_code=404, detail="memory not found in SQLite store")
    return deleted


def _delete_live_memory_hard(request: HardDeleteMemoryRequest) -> dict:
    deleted = _delete_live_memory(MemoryDeleteRequest(id=request.id, project=request.project))
    links = [
        item for item in _load_memory_links()
        if item.get("source_id") != request.id and item.get("target_id") != request.id
    ]
    _save_memory_links(links)
    for loader, saver in (
        (_load_checkpoints, _save_checkpoints),
        (_load_project_maps, _save_project_maps),
        (_load_adrs, _save_adrs),
        (_load_handoffs, _save_handoffs),
        (_load_session_records, _save_session_records),
        (_load_tasks, _save_tasks),
        (_load_task_contexts, _save_task_contexts),
        (_load_risk_registers, _save_risk_registers),
        (_load_validation_snapshots, _save_validation_snapshots),
    ):
        items = loader()
        items = [item for item in items if item.get("memory_id") != request.id]
        saver(items)
    return deleted


def _serialize_memory_record(record: dict) -> dict:
    metadata = dict(record.get("metadata") or {})
    return {
        "id": record.get("source_id"),
        "title": metadata.get("raw_title") or _build_memory_title(record.get("content") or ""),
        "project": record.get("project"),
        "type": record.get("memory_type"),
        "content": record.get("content"),
        "concepts": record.get("tags") or [],
        "files": metadata.get("files") or [],
        "source_system": record.get("source_system"),
        "agent_id": record.get("agent_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "lifecycle": _memory_lifecycle(record),
        "archived_at": metadata.get("archived_at"),
        "archive_reason": metadata.get("archive_reason"),
        "upsert_key": metadata.get("upsert_key"),
        "session_refs": record.get("session_refs") or [],
        "metadata": metadata,
    }


def _serialize_memory_link(link: dict) -> dict:
    return {
        "id": link.get("id"),
        "project": link.get("project"),
        "source_id": link.get("source_id"),
        "target_id": link.get("target_id"),
        "relation": link.get("relation"),
        "created_at": link.get("created_at"),
        "updated_at": link.get("updated_at"),
        "metadata": link.get("metadata") or {},
    }


def _serialize_checkpoint_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "checkpoint_type": record.get("checkpoint_type"),
        "title": record.get("title"),
        "content": record.get("content"),
        "done": record.get("done") or "",
        "next": record.get("next") or "",
        "checks": record.get("checks") or "",
        "risks": record.get("risks") or "",
        "concepts": record.get("concepts") or [],
        "files": record.get("files") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_project_map_record(record: dict) -> dict:
    sections = dict(record.get("sections") or {})
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "auth": sections.get("auth") or "",
        "routing": sections.get("routing") or "",
        "tests": sections.get("tests") or "",
        "deploy": sections.get("deploy") or "",
        "i18n": sections.get("i18n") or "",
        "websocket": sections.get("websocket") or "",
        "risks": sections.get("risks") or "",
        "notes": sections.get("notes") or "",
        "files": record.get("files") or [],
        "concepts": record.get("concepts") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_adr_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "context": record.get("context") or "",
        "decision": record.get("decision") or "",
        "consequences": record.get("consequences") or "",
        "status": record.get("status") or "accepted",
        "files": record.get("files") or [],
        "concepts": record.get("concepts") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_handoff_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "current_state": record.get("current_state") or "",
        "decisions": record.get("decisions") or "",
        "validation": record.get("validation") or "",
        "next_agent_action": record.get("next_agent_action") or "",
        "next_owner_id": record.get("next_owner_id") or "",
        "handoff_sla_deadline": record.get("handoff_sla_deadline") or "",
        "files": record.get("files") or [],
        "concepts": record.get("concepts") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_session_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "done": record.get("done") or "",
        "next": record.get("next") or "",
        "checks": record.get("checks") or "",
        "risks": record.get("risks") or "",
        "decisions": record.get("decisions") or "",
        "files_touched": record.get("files_touched") or [],
        "conversation_notes": record.get("conversation_notes") or "",
        "transcript_ref": record.get("transcript_ref") or "",
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_task_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "task_id": record.get("task_id"),
        "title": record.get("title") or "",
        "intent": record.get("intent") or "",
        "scope_in": record.get("scope_in") or [],
        "scope_out": record.get("scope_out") or [],
        "repo": record.get("repo") or "",
        "owner": record.get("owner") or "",
        "status": record.get("status") or "open",
        "session_id": record.get("session_id") or "",
        "correlation_id": record.get("correlation_id") or "",
        "files_touched": record.get("files_touched") or [],
        "session_record_id": record.get("session_record_id"),
        "memory_id": record.get("memory_id"),
        "done": record.get("done") or "",
        "next": record.get("next") or "",
        "checks": record.get("checks") or "",
        "risks": record.get("risks") or "",
        "decisions": record.get("decisions") or "",
        "validation": record.get("validation") or "",
        "opened_at": record.get("opened_at"),
        "closed_at": record.get("closed_at"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_task_context_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "current_task": record.get("current_task") or "",
        "status": record.get("status") or "",
        "pending_items": record.get("pending_items") or "",
        "guidance": record.get("guidance") or "",
        "next_step": record.get("next_step") or "",
        "files_touched": record.get("files_touched") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_risk_register_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "summary": record.get("summary") or "",
        "top_risks": record.get("top_risks") or [],
        "mitigations": record.get("mitigations") or [],
        "owner": record.get("owner") or "",
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_validation_snapshot_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "lint": record.get("lint") or "",
        "tests": record.get("tests") or "",
        "smoke": record.get("smoke") or "",
        "docs": record.get("docs") or "",
        "overall_status": record.get("overall_status") or "",
        "command_summary": record.get("command_summary") or "",
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_duplicate_candidate(item: dict) -> dict:
    return {
        "left_id": item.get("left_id"),
        "right_id": item.get("right_id"),
        "project": item.get("project"),
        "score": item.get("score"),
        "reason": item.get("reason"),
        "left_title": item.get("left_title"),
        "right_title": item.get("right_title"),
    }


def _serialize_conflict_candidate(item: dict) -> dict:
    return {
        "queue_id": item.get("queue_id"),
        "left_id": item.get("left_id"),
        "right_id": item.get("right_id"),
        "project": item.get("project"),
        "score": item.get("score"),
        "reason": item.get("reason"),
        "left_title": item.get("left_title"),
        "right_title": item.get("right_title"),
        "shared_tags": item.get("shared_tags") or [],
        "left_revision_id": item.get("left_revision_id"),
        "right_revision_id": item.get("right_revision_id"),
        "left_content_sha256": item.get("left_content_sha256"),
        "right_content_sha256": item.get("right_content_sha256"),
    }


def _is_archived_memory(record: dict) -> bool:
    metadata = record.get("metadata") or {}
    lifecycle = str(metadata.get("lifecycle") or "").lower()
    return bool(metadata.get("archived_at") or record.get("archived_at") or lifecycle in {"archived", "deprecated"})


def _memory_lifecycle(record: dict) -> str:
    metadata = record.get("metadata") or {}
    lifecycle = str(record.get("lifecycle") or metadata.get("lifecycle") or "active").strip().lower()
    if lifecycle in {"tombstone", "tombstoned", "purged", "deleted"}:
        return "tombstoned"
    if lifecycle in {"archived", "archive", "deprecated"} or _is_archived_memory(record):
        return "archived"
    return "active"


def _metadata_matches_taxonomy_filters(
    metadata: dict[str, Any],
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = True,
) -> bool:
    lifecycle = str(metadata.get("lifecycle") or "").lower()
    if lifecycle in {"tombstone", "tombstoned", "purged", "deleted"}:
        return False
    if not include_archived and (metadata.get("archived_at") or lifecycle in {"archived", "deprecated"}):
        return False

    record_semantic_type = str(metadata.get("semantic_type") or "").lower()
    if not include_logs and record_semantic_type in {"log", "error"}:
        return False

    if domain and metadata.get("domain") != domain:
        return False
    if semantic_type and metadata.get("semantic_type") != semantic_type:
        return False
    if priority and metadata.get("priority") != priority:
        return False

    return True


def _memory_matches_filters(
    record: dict,
    project: str | None = None,
    memory_type: str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    include_archived: bool = False,
    include_logs: bool = True,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
) -> bool:
    metadata = record.get("metadata") or {}
    record_concepts = set(record.get("tags") or [])
    record_files = set(metadata.get("files") or [])
    accepted_projects = _project_aliases(project)

    if not _metadata_matches_taxonomy_filters(
        metadata,
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
        include_archived=include_archived,
        include_logs=include_logs,
    ):
        return False
    if accepted_projects and record.get("project") not in accepted_projects:
        return False
    if memory_type and record.get("memory_type") != memory_type:
        return False
    if concepts and not set(concepts).issubset(record_concepts):
        return False
    if files and not set(files).issubset(record_files):
        return False
    return True


def _advanced_search_live_memories(request: MemoryAdvancedSearchRequest) -> tuple[list[dict], int]:
    candidates = []
    query = (request.query or "").strip()
    for record in _load_live_memories():
        if not _memory_matches_filters(
            record,
            project=request.project,
            memory_type=request.memory_type,
            concepts=request.concepts,
            files=request.files,
            include_archived=request.include_archived,
            include_logs=request.include_logs,
            domain=request.domain,
            semantic_type=request.semantic_type,
            priority=request.priority,
        ):
            continue

        metadata = record.get("metadata") or {}
        item = {
            "id": record.get("source_id"),
            "memory": record.get("content") or "",
            "metadata": {
                "raw_title": metadata.get("raw_title"),
                "tags": record.get("tags") or [],
                "memory_type": record.get("memory_type"),
                "project": record.get("project"),
                "files": metadata.get("files") or [],
                "archived_at": metadata.get("archived_at"),
                "lifecycle": metadata.get("lifecycle"),
                "domain": metadata.get("domain"),
                "semantic_type": metadata.get("semantic_type"),
                "priority": metadata.get("priority"),
            },
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "score": 0.0,
        }
        score = 0.0
        if query:
            score += _lexical_signal(query, item)
            score += _memory_type_weight(item)
            score += _query_intent_weight(query, item)
            if score <= 0:
                continue
        else:
            score = 1.0
        item["score"] = score
        candidates.append((score, record))

    candidates.sort(
        key=lambda pair: (
            pair[0],
            pair[1].get("updated_at") or pair[1].get("created_at") or "",
        ),
        reverse=True,
    )
    ordered = [record for _, record in candidates]
    total = len(ordered)
    start = max(request.offset, 0)
    end = start + max(min(request.limit, 200), 1)
    window = ordered[start:end]
    _emit_memory_pulses_from_records(window)
    return window, total


def _find_live_memory_by_upsert_key(project: str, upsert_key: str) -> dict | None:
    accepted_projects = _project_aliases(project)
    for item in _load_live_memories():
        metadata = item.get("metadata") or {}
        # Tombstoned/archived records are historical state, not a live
        # idempotency target. A new guarded upsert must create an active
        # record; silently updating a tombstone would make a successful
        # projection invisible to retrieval and leave rollback ambiguous.
        lifecycle = str(item.get("lifecycle") or metadata.get("lifecycle") or "active")
        if (
            item.get("project") in accepted_projects
            and lifecycle == "active"
            and metadata.get("upsert_key") == upsert_key
        ):
            return item
    return None


def _get_memory_links(memory_id: str, project: str) -> list[dict]:
    if _find_live_memory(memory_id, project) is None:
        raise HTTPException(status_code=404, detail="memory not found")
    accepted_projects = _project_aliases(project)
    links = [
        item for item in _load_memory_links()
        if item.get("project") in accepted_projects
        and (item.get("source_id") == memory_id or item.get("target_id") == memory_id)
    ]
    links.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return links


def _recent_activity_live_memories(request: MemoryRecentActivityRequest) -> list[dict]:
    items = [
        item for item in _load_live_memories()
        if _memory_matches_filters(
            item,
            project=request.project,
            memory_type=request.memory_type,
            include_archived=request.include_archived,
        )
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return items[: max(min(request.limit, 200), 1)]


def _load_checkpoints() -> list[dict]:
    path = _checkpoint_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_checkpoints(items: list[dict]) -> Path:
    return _write_json_atomic(_checkpoint_store_path(), items)


def _load_project_maps() -> list[dict]:
    path = _project_map_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_project_maps(items: list[dict]) -> Path:
    return _write_json_atomic(_project_map_store_path(), items)


def _load_adrs() -> list[dict]:
    path = _adr_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_adrs(items: list[dict]) -> Path:
    return _write_json_atomic(_adr_store_path(), items)


def _load_handoffs() -> list[dict]:
    path = _handoff_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_handoffs(items: list[dict]) -> Path:
    return _write_json_atomic(_handoff_store_path(), items)


def _load_session_records() -> list[dict]:
    path = _session_record_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_session_records(items: list[dict]) -> Path:
    return _write_json_atomic(_session_record_store_path(), items)


def _load_tasks() -> list[dict]:
    path = _task_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_tasks(items: list[dict]) -> Path:
    return _write_json_atomic(_task_store_path(), items)


def _load_task_contexts() -> list[dict]:
    path = _task_context_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_task_contexts(items: list[dict]) -> Path:
    return _write_json_atomic(_task_context_store_path(), items)


def _load_risk_registers() -> list[dict]:
    path = _risk_register_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_risk_registers(items: list[dict]) -> Path:
    return _write_json_atomic(_risk_register_store_path(), items)


def _load_validation_snapshots() -> list[dict]:
    path = _validation_snapshot_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_validation_snapshots(items: list[dict]) -> Path:
    return _write_json_atomic(_validation_snapshot_store_path(), items)


def _load_entity_catalogs() -> list[dict]:
    path = _entity_catalog_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_entity_catalogs(items: list[dict]) -> Path:
    return _write_json_atomic(_entity_catalog_store_path(), items)


def _load_policy_profile() -> dict:
    path = _policy_profile_store_path()
    if not path.exists():
        return {
            "max_content_chars": 8000,
            "max_lines": 120,
            "require_project": True,
            "require_memory_type": False,
            "block_secret_like": True,
            "block_raw_logs": False,
            "updated_at": None,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _save_policy_profile(profile: dict) -> Path:
    return _write_json_atomic(_policy_profile_store_path(), profile)


def _load_policy_profile_snapshot() -> dict[str, Any]:
    path = _policy_profile_store_path()
    return {
        "path": str(path),
        "exists": path.exists(),
        "profile": _load_policy_profile(),
    }


def _load_mcp_registry_snapshot() -> dict[str, Any]:
    path = _mcp_registry_snapshot_path()
    snapshot = _read_json_snapshot(path)
    instances = snapshot.get("instances") if isinstance(snapshot, dict) else []
    if not isinstance(instances, list):
        instances = []
    return {
        "path": str(path),
        "loaded": isinstance(snapshot, dict),
        "instance_count": len(instances),
        "snapshot": snapshot,
    }


def _load_slots() -> list[dict]:
    path = _slot_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_slots(items: list[dict]) -> Path:
    return _write_json_atomic(_slot_store_path(), items)


def _load_lessons() -> list[dict]:
    return _safe_json_list(_lesson_store_path())


def _save_lessons(items: list[dict]) -> Path:
    return _write_json_atomic(_lesson_store_path(), items)


def _write_json_atomic(path: Path, payload: object) -> Path:
    with _json_store_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            retry_delays = (*_JSON_REPLACE_RETRY_DELAYS, None)
            for retry_delay in retry_delays:
                try:
                    temp_path.replace(path)
                    break
                except PermissionError:
                    if retry_delay is None:
                        raise
                    time.sleep(retry_delay)
                except OSError as exc:
                    if retry_delay is None or getattr(exc, "winerror", None) not in {5, 32}:
                        raise
                    time.sleep(retry_delay)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
    return path


def _read_boot_report() -> dict[str, Any]:
    try:
        if not _BOOT_REPORT_PATH.exists():
            return {"status": "missing"}
        data = json.loads(_BOOT_REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "ERROR", "error": "boot_report_unavailable", "timestamp": _utc_now_iso()}
    if isinstance(data, dict):
        return data
    return {"status": "ERROR", "error": "boot report is not a JSON object", "timestamp": _utc_now_iso()}


def _boot_report_is_pending() -> bool:
    return _read_boot_report().get("status") == "pending"


def _write_pending_boot_report() -> dict[str, Any]:
    payload = {"status": "pending", "trigger_time": time.time()}
    _write_json_atomic(_BOOT_REPORT_PATH, payload)
    return payload


async def _finalize_pending_boot_report(warmup_task: asyncio.Task[None]) -> None:
    pending = _read_boot_report()
    if pending.get("status") != "pending":
        return

    try:
        trigger_time = float(pending.get("trigger_time") or time.time())
    except (TypeError, ValueError):
        trigger_time = time.time()

    await warmup_task
    await _wait_for_qdrant_ready()
    _write_json_atomic(
        _BOOT_REPORT_PATH,
        {
            "status": "SUCCESS",
            "elapsed_seconds": round(max(time.time() - trigger_time, 0.0), 2),
            "qdrant": "OK",
            "lm_studio": "READY",
            "timestamp": _utc_now_iso(),
        },
    )


def _spawn_detached_restart_launcher() -> int:
    start_script = settings.repo_root / "scripts" / "run-service.ps1"
    log_dir = settings.runtime_dir / "bootstrap"
    log_suffix = f"{os.getpid()}-{int(time.time())}"
    stdout_log = log_dir / f"bhm-restart-{log_suffix}.stdout.log"
    stderr_log = log_dir / f"bhm-restart-{log_suffix}.stderr.log"
    launcher_log = log_dir / f"bhm-restart-{log_suffix}.launcher.log"
    log_dir.mkdir(parents=True, exist_ok=True)

    if os.name != "nt":
        raise HTTPException(status_code=501, detail="detached restart is implemented for Windows runtime only")

    script = f"""
$ErrorActionPreference = "Stop"
Start-Sleep -Milliseconds 1300
"launcher_start $(Get-Date -Format o)" | Set-Content -LiteralPath '{launcher_log}' -Encoding UTF8
Start-Process -FilePath "powershell.exe" -ArgumentList @(
  "-NoProfile",
  "-ExecutionPolicy",
  "Bypass",
  "-File",
  "{start_script}",
  "-SkipInstall"
) -WorkingDirectory "{settings.repo_root}" -WindowStyle Hidden -RedirectStandardOutput "{stdout_log}" -RedirectStandardError "{stderr_log}"
"launcher_done $(Get-Date -Format o)" | Add-Content -LiteralPath '{launcher_log}' -Encoding UTF8
"""
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    # Do not route the restart through ``cmd.exe /c start /min``.  ``/min``
    # only minimizes the console and can still flash a visible PowerShell
    # window during a BHM restart.  Spawn PowerShell directly as a detached,
    # no-console child and keep all output handles closed.
    startupinfo = None
    creationflags = (
        _WINDOWS_DETACHED_PROCESS
        | _WINDOWS_CREATE_NEW_PROCESS_GROUP
        | _WINDOWS_CREATE_BREAKAWAY_FROM_JOB
        | _WINDOWS_CREATE_NO_WINDOW
    )
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_script,
        ],
        cwd=str(settings.repo_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags if os.name == "nt" else 0,
        startupinfo=startupinfo,
        close_fds=True,
    )
    return int(process.pid or 0)


def _register_infra_pid(pid: int | str | None) -> None:
    try:
        normalized = int(pid or 0)
    except (TypeError, ValueError):
        return
    if normalized <= 0 or normalized == os.getpid():
        return
    with _INFRA_SPAWNED_PIDS_LOCK:
        _INFRA_SPAWNED_PIDS.add(normalized)


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
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


async def _terminate_process_tree(pid: int, *, grace_seconds: int = 3, dry_run: bool = False) -> dict[str, Any]:
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
            result["soft_error"] = str(exc)
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
                result["force_error"] = str(exc)
    else:
        try:
            os.kill(pid, 15)
            await asyncio.sleep(max(grace_seconds, 0))
            if _is_pid_running(pid):
                os.kill(pid, 9)
                result["forced"] = True
        except OSError as exc:
            result["error"] = str(exc)
    result["terminated"] = not _is_pid_running(pid)
    return result


async def _cleanup_registered_infra_processes(reason: str = "cleanup", dry_run: bool = False) -> dict[str, Any]:
    with _INFRA_SPAWNED_PIDS_LOCK:
        pids = sorted(_INFRA_SPAWNED_PIDS)
    items = []
    for pid in pids:
        kill_result = await _terminate_process_tree(pid, grace_seconds=3, dry_run=dry_run)
        items.append({"reason": reason, **kill_result})
    if not dry_run:
        with _INFRA_SPAWNED_PIDS_LOCK:
            _INFRA_SPAWNED_PIDS.difference_update(pid for pid in pids if not _is_pid_running(pid))
    return {"reason": reason, "count": len(items), "items": items}


def _safe_json_list(path: Path, repair_trailing_data: bool = False) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        if not repair_trailing_data:
            raise
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(raw)
        if raw[end:].strip():
            _write_json_atomic(path, value)
    if not isinstance(value, list):
        raise ValueError(f"expected JSON list in {path}")
    return value


def _load_observations() -> list[dict]:
    with _OBSERVATION_STORE_LOCK:
        return _observation_store().load()


def _append_observations(items: list[dict]) -> Path:
    """Append observations to the authoritative SQLite journal."""

    with _OBSERVATION_STORE_LOCK:
        store = _observation_store()
        store.initialize()
        store.append_many(items)
        return store.path


def _append_observation(item: dict) -> None:
    with _OBSERVATION_STORE_LOCK:
        try:
            _observation_store().append(item)
        except ObservationIdCollision as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "observation_event_id_collision",
                    "eventId": exc.event_id,
                },
            ) from exc


def _load_memory_links() -> list[dict]:
    return _safe_json_list(_memory_link_store_path())


def _save_memory_links(items: list[dict]) -> Path:
    return _write_json_atomic(_memory_link_store_path(), items)


def _query_tokens(query: str) -> list[str]:
    return [token for token in re.findall(r"[a-zA-Z0-9_-]+", query.lower()) if token]


def _safe_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _memory_type_weight(item: dict) -> float:
    metadata = item.get("metadata") or {}
    tags = set(metadata.get("tags") or [])
    title = (metadata.get("raw_title") or "").lower()

    weight = 0.0
    if "hybrid-session" in tags or "hybrid session" in title:
        weight += 2.5
    if "conversation" in tags:
        weight += 0.5
    if metadata.get("memory_type") == "architecture":
        weight += 0.5
    if metadata.get("memory_type") == "fact":
        weight += 0.2
    if metadata.get("memory_type") == "bug":
        weight += 0.2
    if metadata.get("project"):
        weight += 0.3
    return weight


def _query_intent_weight(query: str, item: dict) -> float:
    metadata = item.get("metadata") or {}
    title = (metadata.get("raw_title") or "").lower()
    tags = set(metadata.get("tags") or [])
    tokens = set(_query_tokens(query))
    memory_type = metadata.get("memory_type")

    score = 0.0
    checkpoint_like = {"checkpoint", "session", "hybrid", "workflow"}
    if tokens & checkpoint_like:
        if "checkpoint" in title:
            score += 1.5
        if "hybrid-session" in tags:
            score += 1.5
    else:
        if memory_type in {"fact", "bug", "pattern"}:
            score += 1.4
        if "hybrid-session" in tags:
            score -= 0.3
        if "checkpoint" in title:
            score -= 0.6

    if "role" in tokens and "capability" in tokens and "hybrid-session" in tags:
        score += 1.5

    if {"codex", "mcp", "tools"}.issubset(tokens):
        content = (item.get("memory") or "").lower()
        if "tool_search" in content or "plugin" in title or "plugin" in content:
            score += 1.8
        if "figma mcp" in tags or "codex plugin" in tags:
            score += 3.2
        if "mcp preflight" in tags or "oz mcp list" in tags:
            score -= 1.3
        if "connectivity" in tags:
            score -= 0.4

    if {"workspace", "memory", "checkpoint"}.issubset(tokens):
        if "workspace-layers" in tags:
            score += 4.0
        if "project.md" in title or "project.md" in (item.get("memory") or "").lower():
            score += 3.0
        if "hub" in tags:
            score += 1.5
        if "checkpoint" in title and "workspace-layers" not in tags:
            score -= 0.4

    if {"blackholememory", "checkpoint"}.issubset(tokens):
        content = (item.get("memory") or "").lower()
        if "created new blackholememory repo" in content:
            score += 3.0
        if "bootstrap" in content or "bootstrap" in title:
            score += 1.5
        if "initial qdrant compose" in content:
            score += 1.0

    return score


def _mem0_user_ids_for_search(user_id: str) -> list[str]:
    return [user_id]


def _vector_item_matches_search_request(item: dict, request: SearchRequest) -> bool:
    metadata = item.get("metadata") or {}
    metadata_source_id = metadata.get("source_id")
    live_record = _resolve_live_memory_for_vector_item(item, request.project)
    if live_record is not None:
        item["metadata"] = _mem0_metadata_for_record(live_record)
        return _memory_matches_filters(
            live_record,
            project=request.project,
            include_archived=request.include_archived,
            include_logs=request.include_logs,
            domain=request.domain,
            semantic_type=request.semantic_type,
            priority=request.priority,
        )
    if metadata_source_id:
        return False

    if request.project:
        accepted_projects = _project_aliases(request.project)
        if metadata.get("project") not in accepted_projects:
            return False
    return _metadata_matches_taxonomy_filters(
        metadata,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        include_archived=request.include_archived,
        include_logs=request.include_logs,
    )


def _resolve_live_memory_for_vector_item(item: dict, project: str | None = None) -> dict | None:
    metadata = item.get("metadata") or {}
    candidate_ids = [
        metadata.get("source_id"),
        metadata.get("id"),
        item.get("source_id"),
        item.get("id"),
        item.get("hash"),
    ]
    for candidate_id in candidate_ids:
        if not candidate_id:
            continue
        record = _find_live_memory(str(candidate_id), project)
        if record is not None:
            return record

    return None


def _lexical_signal(query: str, item: dict) -> float:
    metadata = item.get("metadata") or {}
    text = (item.get("memory") or "").lower()
    title = (metadata.get("raw_title") or "").lower()
    tag_blob = " ".join(metadata.get("tags") or []).lower()
    tokens = _query_tokens(query)

    score = 0.0
    if query.lower() in title:
        score += 2.0
    if query.lower() in text:
        score += 1.0

    for token in tokens:
        if token in title:
            score += 0.8
        if token in tag_blob:
            score += 0.8
        if token in text:
            score += 0.25

    return score


def _collapse_duplicates(results: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for item in results:
        key = item.get("hash") or item.get("memory") or item.get("id")
        current = grouped.get(key)
        if current is None:
            grouped[key] = item
            continue

        current_project = (current.get("metadata") or {}).get("project")
        item_project = (item.get("metadata") or {}).get("project")
        current_created = _safe_datetime(current.get("created_at"))
        item_created = _safe_datetime(item.get("created_at"))

        replace = False
        if not current_project and item_project:
            replace = True
        elif current_project == item_project and item_created < current_created:
            replace = True

        if replace:
            grouped[key] = item

    return list(grouped.values())


def _build_memory_title(content: str) -> str:
    first_line = (content or "").strip().splitlines()[0] if (content or "").strip() else "memory"
    return first_line[:80]


_PROTECTED_MEMORY_METADATA_KEYS = frozenset(
    {
        "project",
        "source_system",
        "source_id",
        "memory_type",
        "tags",
        "session_refs",
        "created_at",
        "updated_at",
        "raw_title",
        "confidence",
        "files",
        "upsert_key",
        "archived_at",
        "archive_reason",
        "changelog",
        "mem0_ids",
        "access_count",
        "last_accessed_at",
    }
)


def _metadata_to_dict(metadata: MemoryMetadata | dict[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if isinstance(metadata, BaseModel):
        if hasattr(metadata, "model_dump"):
            return metadata.model_dump(mode="json", exclude_none=True)
        return metadata.dict(exclude_none=True)
    return dict(metadata)


def _user_memory_metadata(metadata: MemoryMetadata | dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    metadata_payload = _metadata_to_dict(metadata)
    return {
        key: value
        for key, value in metadata_payload.items()
        if key not in _PROTECTED_MEMORY_METADATA_KEYS
    }


def _semantic_graph_node_id(item: dict) -> str:
    metadata = item.get("metadata") or {}
    return str(metadata.get("source_id") or item.get("source_id") or item.get("id") or "").strip()


def _semantic_graph_node_aliases(item: dict) -> list[str]:
    metadata = item.get("metadata") or {}
    aliases = [
        metadata.get("source_id"),
        item.get("source_id"),
        item.get("id"),
        metadata.get("mem0_hit_id"),
    ]
    return list(dict.fromkeys(str(alias).strip() for alias in aliases if str(alias or "").strip()))


def _mem0_metadata_for_record(record: dict) -> dict[str, Any]:
    return {
        "project": record["project"],
        "source_system": record["source_system"],
        "source_id": record["source_id"],
        "memory_type": record["memory_type"],
        "tags": record["tags"],
        "session_refs": record["session_refs"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        **(record.get("metadata") or {}),
    }


def _extract_mem0_ids(add_result: Any) -> list[str]:
    if not isinstance(add_result, dict):
        return []
    return [
        str(item["id"])
        for item in add_result.get("results") or []
        if isinstance(item, dict) and item.get("id")
    ]


def _vector_targets_for_record(record: dict) -> list[str]:
    return list(route_vector_targets(record).targets)


def _vector_id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    return [str(item) for item in items if str(item).strip()]


def _vector_metadata_for_record(
    record: dict,
    *,
    collection_name: str,
    context_origin: str,
    vector_targets: list[str],
) -> dict[str, Any]:
    metadata = _mem0_metadata_for_record(record)
    metadata["context_origin"] = context_origin
    metadata["context_origins"] = [context_origin]
    metadata["vector_collection"] = collection_name
    metadata["vector_targets"] = vector_targets
    metadata["vector_scope"] = "local+global" if "global" in vector_targets else "local"
    return metadata


def _write_vector_record(
    *,
    record: dict,
    collection_name: str,
    context_origin: str,
    vector_targets: list[str],
    existing_ids: list[str],
) -> list[str]:
    memory = get_global_core_memory() if context_origin == _VECTOR_CONTEXT_GLOBAL else get_project_mem0_memory(record.get("project"))
    metadata = _vector_metadata_for_record(
        record,
        collection_name=collection_name,
        context_origin=context_origin,
        vector_targets=vector_targets,
    )
    content = record.get("content") or ""
    agent_id = record.get("agent_id") or "workspace"
    updated_ids: list[str] = []
    for mem0_id in existing_ids:
        try:
            memory.update(mem0_id, data=content, metadata=metadata)
        except Exception:
            continue
        updated_ids.append(mem0_id)
    if updated_ids:
        return updated_ids

    add_result = memory.add(
        [{"role": "user", "content": content}],
        user_id=settings.mem0_user_id,
        agent_id=agent_id,
        metadata=metadata,
        infer=False,
    )
    return _extract_mem0_ids(add_result)


def _sync_mem0_record(record: dict) -> dict[str, list[str]]:
    metadata = ensure_decay_metadata(
        record.setdefault("metadata", {}),
        fallback_at=record.get("created_at") or record.get("updated_at") or _utc_now_iso(),
    )
    record["metadata"] = metadata
    vector_targets = _vector_targets_for_record(record)
    global_ids_existing = _vector_id_list(metadata.get("global_mem0_ids"))
    if global_ids_existing and "global" not in vector_targets:
        vector_targets.append("global")

    local_collection = local_collection_name(record.get("project"))
    if _memory_store_is_authoritative():
        # SQLite is the write authority after cutover.  Mem0's ``add``/``update``
        # methods write directly to Qdrant and therefore bypass the transactional
        # outbox, creating orphan projections when the explicit projector is
        # intentionally offline.  Keep routing metadata for the queued projector,
        # but never mutate Mem0/Qdrant from an authoritative route.
        metadata["vector_targets"] = vector_targets
        metadata["vector_scope"] = "local+global" if "global" in vector_targets else "local"
        vector_collections = [local_collection]
        if "global" in vector_targets:
            vector_collections.append(global_collection_name())
        metadata["vector_collections"] = vector_collections
        return {
            "local": _vector_id_list(metadata.get("mem0_ids")),
            "global": global_ids_existing if "global" in vector_targets else [],
        }

    local_ids = _write_vector_record(
        record=record,
        collection_name=local_collection,
        context_origin=_VECTOR_CONTEXT_LOCAL,
        vector_targets=vector_targets,
        existing_ids=_vector_id_list(metadata.get("mem0_ids")),
    )
    metadata["mem0_ids"] = local_ids

    global_ids: list[str] = []
    if "global" in vector_targets:
        global_ids = _write_vector_record(
            record=record,
            collection_name=global_collection_name(),
            context_origin=_VECTOR_CONTEXT_GLOBAL,
            vector_targets=vector_targets,
            existing_ids=global_ids_existing,
        )
        metadata["global_mem0_ids"] = global_ids

    vector_collections = [local_collection]
    if "global" in vector_targets:
        vector_collections.append(global_collection_name())
    metadata["vector_targets"] = vector_targets
    metadata["vector_scope"] = "local+global" if "global" in vector_targets else "local"
    metadata["vector_collections"] = vector_collections
    return {"local": local_ids, "global": global_ids}


def _update_mem0_record(record: dict) -> None:
    _sync_mem0_record(record)


def _remember_live_memory(request: RememberRequest) -> dict:
    source_id = f"mem_bhm_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    project = _canonical_project(request.project)
    metadata = initial_decay_metadata(
        {
            "raw_title": _build_memory_title(request.content),
            "confidence": None,
            "files": request.files or [],
            "upsert_key": request.upsert_key,
            **_user_memory_metadata(request.metadata),
        },
        created_at=now,
    )
    record = {
        "source_system": "bhm",
        "source_id": source_id,
        "project": project,
        "agent_id": "workspace",
        "memory_type": request.type,
        "content": request.content,
        "summary": None,
        "tags": request.concepts or [],
        "session_refs": [],
        "created_at": now,
        "updated_at": now,
        "metadata": metadata,
    }

    live_records = _load_live_memories()
    live_records.append(record)
    _sync_mem0_record(record)
    live_records[-1] = record
    _save_live_memories(live_records)
    return record


def _update_live_memory(request: MemoryUpdateRequest) -> dict:
    live_records = _load_live_memories()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    canonical_project = _canonical_project(request.project) if request.project else None
    accepted_projects = _project_aliases(request.project)

    for item in live_records:
        if item.get("source_id") != request.id:
            continue
        if request.project and item.get("project") not in accepted_projects:
            continue

        if canonical_project:
            item["project"] = canonical_project
        if request.type:
            item["memory_type"] = request.type
        if request.content is not None:
            item["content"] = request.content
            item.setdefault("metadata", {})["raw_title"] = _build_memory_title(request.content)
        if request.concepts is not None:
            item["tags"] = request.concepts
        if request.files is not None:
            item.setdefault("metadata", {})["files"] = request.files
        if request.metadata_patch is not None:
            item.setdefault("metadata", {}).update(_user_memory_metadata(request.metadata_patch))

        item["updated_at"] = now
        item["metadata"] = ensure_decay_metadata(
            item.setdefault("metadata", {}),
            fallback_at=item.get("created_at") or now,
        )
        _append_memory_changelog(
            item,
            "update",
            {
                "type": request.type,
                "content_changed": request.content is not None,
                "metadata_changed": request.metadata_patch is not None,
            },
        )
        _update_mem0_record(item)
        _save_live_memories(live_records)
        return item

    raise HTTPException(status_code=404, detail="memory not found in live store")


def _archive_live_memory(request: MemoryArchiveRequest) -> dict:
    live_records = _load_live_memories()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    accepted_projects = _project_aliases(request.project)

    for item in live_records:
        if item.get("source_id") != request.id:
            continue
        if request.project and item.get("project") not in accepted_projects:
            continue

        metadata = item.setdefault("metadata", {})
        metadata["archived_at"] = now
        metadata["archive_reason"] = request.reason
        item["updated_at"] = now
        _append_memory_changelog(item, "archive", {"reason": request.reason})
        _save_live_memories(live_records)
        _emit_memory_pulse(item.get("source_id"), str(item.get("project") or ""))
        return item

    raise HTTPException(status_code=404, detail="memory not found in live store")


def _upsert_live_memory(request: MemoryUpsertRequest) -> tuple[str, dict]:
    canonical_project = _canonical_project(request.project)
    existing = _find_live_memory_by_upsert_key(canonical_project, request.upsert_key)
    if existing is None:
        record = _remember_live_memory(
            RememberRequest(
                project=canonical_project,
                type=request.type,
                content=request.content,
                concepts=request.concepts,
                files=request.files,
                upsert_key=request.upsert_key,
                metadata=request.metadata,
            )
        )
        return "created", record

    metadata_patch = _user_memory_metadata(request.metadata) if request.metadata is not None else None
    updated = _update_live_memory(
        MemoryUpdateRequest(
            id=existing["source_id"],
            project=canonical_project,
            type=request.type,
            content=request.content,
            concepts=request.concepts,
            files=request.files,
            metadata_patch=metadata_patch,
        )
    )
    updated.setdefault("metadata", {})["upsert_key"] = request.upsert_key
    live_records = _load_live_memories()
    for item in live_records:
        if item.get("source_id") == updated.get("source_id"):
            item_metadata = item.setdefault("metadata", {})
            if metadata_patch is not None:
                item_metadata.update(metadata_patch)
            item_metadata["upsert_key"] = request.upsert_key
            item["metadata"] = ensure_decay_metadata(item_metadata, fallback_at=item.get("created_at") or item.get("updated_at"))
            break
    _save_live_memories(live_records)
    if metadata_patch is not None:
        updated.setdefault("metadata", {}).update(metadata_patch)
    updated["metadata"]["upsert_key"] = request.upsert_key
    updated["metadata"] = ensure_decay_metadata(
        updated["metadata"],
        fallback_at=updated.get("created_at") or updated.get("updated_at"),
    )
    return "updated", updated


async def _semantic_dependency_target_id(project_name: str, source_id: str, keyword: str) -> str | None:
    search_kwargs = {
        "limit": 5,
        "offset": 0,
        "include_archived": False,
        "include_logs": False,
        "include_graph_expansion": False,
    }
    for memory_type in ("knowledge-crystal", None):
        hits, _total = await federated_search(
            keyword,
            project_name,
            memory_type=memory_type,
            **search_kwargs,
        )
        for hit in hits:
            candidate_id = _semantic_graph_node_id(hit)
            if candidate_id and candidate_id != source_id:
                return candidate_id
    return None


async def _add_semantic_dependency_links(record: dict, project_name: str) -> dict[str, Any]:
    source_id = str(record.get("source_id") or "").strip()
    metadata = record.get("metadata") or {}
    dependencies = _normalize_linked_dependencies(metadata.get("linked_dependencies"))
    if not source_id or not dependencies:
        return {"created": [], "errors": []}

    created: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for dependency in dependencies:
        keyword = dependency["target_core_insight_keyword"]
        edge_type = dependency["edge_type"]
        try:
            target_id = await _semantic_dependency_target_id(project_name, source_id, keyword)
            if not target_id:
                errors.append({"keyword": keyword, "edge_type": edge_type, "error": "target_not_found"})
                continue
            edge = await _BHM_GRAPH_MANAGER.add_semantic_link(source_id, target_id, edge_type)
            created.append({"source_id": source_id, **edge, "keyword": keyword})
        except Exception as exc:
            errors.append({"keyword": keyword, "edge_type": edge_type, "error": str(exc)})
    return {"created": created, "errors": errors}


def _create_memory_link(request: MemoryLinkRequest) -> dict:
    if request.source_id == request.target_id:
        raise HTTPException(status_code=400, detail="source_id and target_id must differ")
    if _find_live_memory(request.source_id, request.project) is None:
        raise HTTPException(status_code=404, detail="source memory not found")
    if _find_live_memory(request.target_id, request.project) is None:
        raise HTTPException(status_code=404, detail="target memory not found")

    links = _load_memory_links()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for item in links:
        if (
            item.get("project") == request.project
            and item.get("source_id") == request.source_id
            and item.get("target_id") == request.target_id
            and item.get("relation") == request.relation
        ):
            item["updated_at"] = now
            item["metadata"] = _metadata_to_dict(request.metadata) or item.get("metadata") or {}
            _save_memory_links(links)
            return item

    link = {
        "id": f"link_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "source_id": request.source_id,
        "target_id": request.target_id,
        "relation": request.relation,
        "created_at": now,
        "updated_at": now,
        "metadata": _metadata_to_dict(request.metadata),
    }
    links.append(link)
    _save_memory_links(links)
    return link


def _delete_memory_link(request: MemoryLinkDeleteRequest) -> bool:
    links = _load_memory_links()
    remaining = [
        item for item in links
        if not (
            item.get("project") == request.project
            and item.get("source_id") == request.source_id
            and item.get("target_id") == request.target_id
            and item.get("relation") == request.relation
        )
    ]
    _save_memory_links(remaining)
    return len(remaining) != len(links)


def _crystallize_memories(request: MemoryCrystallizeRequest) -> tuple[str, dict]:
    if len(request.source_ids) < 1:
        raise HTTPException(status_code=400, detail="source_ids must not be empty")

    source_records = []
    for memory_id in request.source_ids:
        record = _find_live_memory(memory_id, request.project)
        if record is None:
            raise HTTPException(status_code=404, detail=f"source memory not found: {memory_id}")
        source_records.append(record)

    source_titles = [
        (record.get("metadata") or {}).get("raw_title") or _build_memory_title(record.get("content") or "")
        for record in source_records
    ]
    source_files = sorted(
        {
            file_path
            for record in source_records
            for file_path in ((record.get("metadata") or {}).get("files") or [])
        }
    )
    files = request.files if request.files is not None else source_files

    lines = [
        f"{request.title}",
        f"summary: {request.summary}",
        "source_ids:",
    ]
    for source_id in request.source_ids:
        lines.append(f"- {source_id}")
    if source_titles:
        lines.append("source_titles:")
        for title in source_titles:
            lines.append(f"- {title}")
    content = "\n".join(lines)

    if request.upsert_key:
        action, record = _upsert_live_memory(
            MemoryUpsertRequest(
                upsert_key=request.upsert_key,
                project=request.project,
                type=request.target_type,
                content=content,
                concepts=request.concepts,
                files=files,
            )
        )
    else:
        record = _remember_live_memory(
            RememberRequest(
                project=request.project,
                type=request.target_type,
                content=content,
                concepts=request.concepts,
                files=files,
            )
        )
        action = "created"

    metadata = record.setdefault("metadata", {})
    metadata["crystallized_from"] = request.source_ids
    metadata["crystallized_summary"] = request.summary
    metadata["crystallized_title"] = request.title
    if request.upsert_key:
        metadata["upsert_key"] = request.upsert_key

    live_records = _load_live_memories()
    for item in live_records:
        if item.get("source_id") == record.get("source_id"):
            item.setdefault("metadata", {}).update(metadata)
            break
    _save_live_memories(live_records)
    for source_id in request.source_ids:
        _emit_memory_pulse(source_id, request.project)
    _emit_memory_pulse(record.get("source_id"), request.project)
    return action, record


_HOOK_TRANSIT_SOURCE_LIMIT = 12
_HOOK_TRANSIT_ITEM_CHAR_LIMIT = 4000
_HOOK_SCRIPT_MODULE_CACHE: dict[str, Any] = {}
_HOOK_SCRIPT_MODULE_LOCK = threading.RLock()


def _dedupe_text(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _coerce_hook_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in (chunk.strip() for chunk in re.split(r"[,\n]+", value)) if part]
    if isinstance(value, (list, tuple)):
        return _dedupe_text([str(item) for item in value if item is not None])
    return []


def _hook_data_value(request: BhmHookRequest, *keys: str) -> Any:
    data = request.data or {}
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _hook_timestamp(request: BhmHookRequest) -> str:
    return request.timestamp or _utc_now_iso()


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
    _append_observation(item)
    return item


def _hook_text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _hook_transit_items(request: BhmHookCompactRequest) -> list[Any]:
    explicit_values = [
        request.transit_buffer,
        _hook_data_value(request, "transit_buffer", "transitBuffer"),
        _hook_data_value(request, "transit", "buffer", "current_buffer", "currentBuffer"),
        _hook_data_value(request, "messages", "history", "events", "transcript", "context"),
    ]
    items: list[Any] = []
    for value in explicit_values:
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            items.extend(value)
        else:
            items.append(value)
    if not items and request.data:
        items.append(dict(request.data))
    return items[:_HOOK_TRANSIT_SOURCE_LIMIT]


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


def _extract_hook_source_ids(request: BhmHookCompactRequest, transit_items: list[Any]) -> list[str]:
    source_ids: list[str] = []
    source_ids.extend(_coerce_hook_text_list(request.source_ids))
    for key in ("source_ids", "sourceIds", "memory_ids", "memoryIds"):
        source_ids.extend(_coerce_hook_text_list(_hook_data_value(request, key)))
    for item in transit_items:
        if not isinstance(item, dict):
            continue
        source_ids.extend(
            _coerce_hook_text_list(
                item.get("source_id")
                or item.get("sourceId")
                or item.get("memory_id")
                or item.get("memoryId")
            )
        )
    return _dedupe_text(source_ids)


def _hook_compact_concepts(request: BhmHookCompactRequest) -> list[str]:
    concepts = [
        "bhm",
        "codex-hook",
        "pre-compact",
        "reflex",
        "crystallization",
    ]
    concepts.extend(_coerce_hook_text_list(request.concepts))
    concepts.extend(_coerce_hook_text_list(_hook_data_value(request, "concepts", "tags")))
    return _dedupe_text(concepts)


def _hook_compact_files(request: BhmHookCompactRequest) -> list[str]:
    files = _coerce_hook_text_list(request.files)
    files.extend(_coerce_hook_text_list(_hook_data_value(request, "files", "files_touched", "filesTouched")))
    return _dedupe_text(files)


def _materialize_hook_transit_sources(request: BhmHookCompactRequest, transit_items: list[Any]) -> list[dict]:
    records: list[dict] = []
    concepts = _hook_compact_concepts(request)
    files = _hook_compact_files(request)
    for index, item in enumerate(transit_items[:_HOOK_TRANSIT_SOURCE_LIMIT]):
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
                f"index: {index}",
                "payload:",
                content,
            ]
        )
        _, record = _upsert_live_memory(
            MemoryUpsertRequest(
                upsert_key=f"hook-compact-source:{request.project}:{request.sessionId}:{index}",
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
        or _hook_text_or_none(_hook_data_value(request, "title"))
        or f"{request.project} pre-compact reflex crystal"
    )
    summary = (
        _hook_text_or_none(request.summary)
        or _hook_text_or_none(_hook_data_value(request, "summary", "reason"))
        or f"Emergency Codex pre-compact reflex captured {len(source_ids)} source item(s)."
    )
    upsert_key = (
        _hook_text_or_none(request.upsert_key)
        or _hook_text_or_none(_hook_data_value(request, "upsert_key", "upsertKey"))
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
    observation = _append_hook_observation(request, "compact")
    transit_items = _hook_transit_items(request)
    source_ids = _extract_hook_source_ids(request, transit_items)
    materialized_sources: list[dict] = []

    if not source_ids:
        materialized_sources = _materialize_hook_transit_sources(request, transit_items)
        source_ids = _dedupe_text([str(record.get("source_id") or "") for record in materialized_sources])

    if not source_ids:
        return {
            "success": True,
            "action": "skipped",
            "reason": "empty_transit_buffer",
            "hook": {"type": request.hookType, "sessionId": request.sessionId, "project": request.project},
            "observation": {"id": observation.get("id")},
        }

    action, record = _crystallize_memories(_build_hook_crystallize_request(request, source_ids))
    return {
        "success": True,
        "action": action,
        "hook": {"type": request.hookType, "sessionId": request.sessionId, "project": request.project},
        "source_ids": source_ids,
        "materialized_source_ids": [record.get("source_id") for record in materialized_sources],
        "memory": _serialize_memory_record(record),
        "observation": {"id": observation.get("id")},
    }


def _apply_idle_duplicate_merge(request: BhmHookIdleRequest) -> dict:
    candidates = _detect_duplicates(
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
            merged.append({"error": str(exc), "candidate": candidate})
    return {"candidates": len(candidates), "merged": merged}


def _refresh_decay_scores_for_project(project: str, limit: int) -> dict:
    client = get_qdrant_client()
    now_dt = datetime.now(timezone.utc)
    recalculated_at = now_dt.isoformat().replace("+00:00", "Z")
    updated = 0
    scanned = 0
    errors: list[dict[str, str]] = []
    collection_names = _dedupe_text([local_collection_name(project), global_collection_name()])

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
                errors.append({"collection": collection_name, "error": str(exc)})
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
                    errors.append({"collection": collection_name, "point_id": str(getattr(point, "id", "")), "error": str(exc)})
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
    result: dict[str, Any] = {
        "success": True,
        "hook": {"type": request.hookType, "sessionId": request.sessionId, "project": request.project},
        "started_at": _utc_now_iso(),
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
            result["steps"][step_name] = {"success": False, "error": str(exc)}

    for step_name, step_call in (
        ("graph_healer", lambda: _run_idle_graph_healer(request)),
        ("reflection_daemon", lambda: _run_idle_reflection_daemon(request)),
    ):
        try:
            result["steps"][step_name] = await step_call()
        except Exception as exc:
            result["steps"][step_name] = {"success": False, "error": str(exc)}

    result["finished_at"] = _utc_now_iso()
    return result


def _build_checkpoint_content(request: CheckpointCreateRequest) -> str:
    if request.content and request.content.strip():
        return request.content.strip()
    lines = [
        f"{request.project} checkpoint:",
        f"done: {request.done}",
        f"next: {request.next}",
        f"checks: {request.checks}",
        f"risks/notes: {request.risks}",
    ]
    return "\n".join(lines)


def _checkpoint_upsert_key(request: CheckpointCreateRequest, project: str, title: str) -> str:
    if request.upsert_key and request.upsert_key.strip():
        return request.upsert_key.strip()
    checkpoint_type = re.sub(r"[^a-z0-9._-]+", "-", (request.checkpoint_type or "workflow").strip().lower()).strip("-")
    title_slug = re.sub(r"[^a-z0-9._-]+", "-", title.strip().lower()).strip("-")
    return f"checkpoint:{project}:{checkpoint_type or 'workflow'}:{title_slug or 'checkpoint'}"


def _create_checkpoint(request: CheckpointCreateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    content = _build_checkpoint_content(request)
    title = (request.title or "").strip() or f"{request.project} checkpoint"
    concepts = list(dict.fromkeys((request.concepts or []) + ["checkpoint", "bhm"]))
    files = request.files or []
    project = _canonical_project(request.project)
    upsert_key = _checkpoint_upsert_key(request, project, title)

    action, memory_record = _upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=project,
            type=request.checkpoint_type,
            content=content,
            concepts=concepts,
            files=files,
        )
    )

    checkpoints = _load_checkpoints()
    for item in checkpoints:
        metadata = item.get("metadata") or {}
        if item.get("project") in _project_aliases(project) and metadata.get("upsert_key") == upsert_key:
            item.update(
                {
                    "checkpoint_type": request.checkpoint_type,
                    "title": title,
                    "content": content,
                    "done": request.done,
                    "next": request.next,
                    "checks": request.checks,
                    "risks": request.risks,
                    "concepts": concepts,
                    "files": files,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            item["project"] = project
            item.setdefault("metadata", {})["upsert_key"] = upsert_key
            item["metadata"]["artifact_kind"] = "checkpoint"
            _save_checkpoints(checkpoints)
            return action, item

    checkpoint = {
        "id": f"checkpoint_bhm_{uuid.uuid4().hex[:16]}",
        "project": project,
        "checkpoint_type": request.checkpoint_type,
        "title": title,
        "content": content,
        "done": request.done,
        "next": request.next,
        "checks": request.checks,
        "risks": request.risks,
        "concepts": concepts,
        "files": files,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "upsert_key": upsert_key,
            "artifact_kind": "checkpoint",
        },
    }
    checkpoints.append(checkpoint)
    _save_checkpoints(checkpoints)
    return action, checkpoint


def _list_checkpoints(project: str | None, checkpoint_type: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = _load_checkpoints()
    if project:
        items = [item for item in items if item.get("project") == project]
    if checkpoint_type:
        items = [item for item in items if item.get("checkpoint_type") == checkpoint_type]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _get_latest_checkpoint(project: str, checkpoint_type: str | None = None) -> dict:
    items, total = _list_checkpoints(project=project, checkpoint_type=checkpoint_type, limit=1, offset=0)
    if total < 1 or not items:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    return items[0]


def _build_project_map_sections(request: ProjectMapUpsertRequest) -> dict[str, str]:
    return {
        "auth": request.auth,
        "routing": request.routing,
        "tests": request.tests,
        "deploy": request.deploy,
        "i18n": request.i18n,
        "websocket": request.websocket,
        "risks": request.risks,
        "notes": request.notes,
    }


def _build_project_map_content(title: str, sections: dict[str, str]) -> str:
    lines = [title]
    for key in ("auth", "routing", "tests", "deploy", "i18n", "websocket", "risks", "notes"):
        lines.append(f"{key}: {sections.get(key) or ''}")
    return "\n".join(lines)


def _get_project_map(project: str) -> dict:
    for item in _load_project_maps():
        if item.get("project") == project:
            return item
    raise HTTPException(status_code=404, detail="project map not found")


def _upsert_project_map(request: ProjectMapUpsertRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = (request.title or "").strip() or f"{request.project} project map"
    sections = _build_project_map_sections(request)
    concepts = list(dict.fromkeys((request.concepts or []) + ["project-map", "bhm"]))
    files = request.files or []
    upsert_key = request.upsert_key or f"project-map:{request.project}"
    content = _build_project_map_content(title, sections)

    action, memory_record = _upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="architecture",
            content=content,
            concepts=concepts,
            files=files,
        )
    )

    maps = _load_project_maps()
    for item in maps:
        metadata = item.get("metadata") or {}
        if item.get("project") == request.project or metadata.get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": title,
                    "sections": sections,
                    "files": files,
                    "concepts": concepts,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            item.setdefault("metadata", {})["upsert_key"] = upsert_key
            _save_project_maps(maps)
            return action, item

    project_map = {
        "id": f"project_map_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": title,
        "sections": sections,
        "files": files,
        "concepts": concepts,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "upsert_key": upsert_key,
        },
    }
    maps.append(project_map)
    _save_project_maps(maps)
    return action, project_map


def _merge_memories(request: MemoryMergeRequest) -> dict:
    if request.source_id == request.target_id:
        raise HTTPException(status_code=400, detail="source_id and target_id must differ")
    source = _find_live_memory(request.source_id, request.project)
    target = _find_live_memory(request.target_id, request.project)
    if source is None:
        raise HTTPException(status_code=404, detail="source memory not found")
    if target is None:
        raise HTTPException(status_code=404, detail="target memory not found")

    source_metadata = dict(source.get("metadata") or {})
    target_metadata = target.setdefault("metadata", {})
    source_files = set(source_metadata.get("files") or [])
    target_files = set(target_metadata.get("files") or [])
    source_tags = set(source.get("tags") or [])
    target_tags = set(target.get("tags") or [])
    merged_lines = [
        target.get("content") or "",
        "",
        "merged_memory:",
        f"- source_id: {source.get('source_id')}",
        f"- source_title: {source_metadata.get('raw_title') or _build_memory_title(source.get('content') or '')}",
        "source_content:",
        source.get("content") or "",
    ]
    target["content"] = "\n".join(line for line in merged_lines if line is not None).strip()
    target["tags"] = sorted(target_tags | source_tags)
    target_metadata["files"] = sorted(target_files | source_files)
    target_metadata["raw_title"] = _build_memory_title(target.get("content") or "")
    target_metadata["merged_from"] = sorted(
        set(target_metadata.get("merged_from") or []) | {source.get("source_id")}
    )
    target["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _replace_live_memory(target)

    if request.archive_source:
        source = _archive_live_memory(
            MemoryArchiveRequest(
                id=request.source_id,
                project=request.project,
                reason=f"merged_into:{request.target_id}",
            )
        )
        source.setdefault("metadata", {})["merged_into"] = request.target_id
        _replace_live_memory(source)

    return {
        "target": target,
        "source": source,
        "archived_source": request.archive_source,
    }


def _detect_duplicates(request: MemoryDetectRequest) -> list[dict]:
    records = [
        item for item in _load_live_memories()
        if _memory_matches_filters(item, project=request.project, include_archived=request.include_archived)
    ]
    candidates: list[dict] = []
    for left_index, left in enumerate(records):
        for right in records[left_index + 1:]:
            if left.get("project") != right.get("project"):
                continue
            left_title = ((left.get("metadata") or {}).get("raw_title") or "").strip()
            right_title = ((right.get("metadata") or {}).get("raw_title") or "").strip()
            left_text = _normalized_text(left.get("content"))
            right_text = _normalized_text(right.get("content"))
            left_files = set((left.get("metadata") or {}).get("files") or [])
            right_files = set((right.get("metadata") or {}).get("files") or [])

            score = 0.0
            reason = ""
            if left_text and left_text == right_text:
                score = 1.0
                reason = "identical_content"
            elif left_title and left_title.lower() == right_title.lower() and left_files == right_files and left_files:
                score = 0.92
                reason = "same_title_same_files"
            elif left_title and left_title.lower() == right_title.lower():
                score = 0.82
                reason = "same_title"
            elif left_files and right_files and left_files == right_files and left_text[:120] == right_text[:120]:
                score = 0.78
                reason = "same_files_similar_prefix"

            if score <= 0:
                continue
            candidates.append(
                {
                    "left_id": left.get("source_id"),
                    "right_id": right.get("source_id"),
                    "project": left.get("project"),
                    "score": score,
                    "reason": reason,
                    "left_title": left_title or _build_memory_title(left.get("content") or ""),
                    "right_title": right_title or _build_memory_title(right.get("content") or ""),
                }
            )
    candidates.sort(key=lambda item: (item.get("score") or 0.0, item.get("left_title") or ""), reverse=True)
    return candidates[: max(min(request.limit, 200), 1)]


def _memory_content_sha256(record: dict) -> str:
    metadata = record.get("metadata") or {}
    return str(metadata.get("content_sha256") or hashlib.sha256(str(record.get("content") or "").encode("utf-8")).hexdigest())


def _review_status(record: dict) -> str:
    value = str((record.get("metadata") or {}).get("review_status") or "open").strip().lower()
    return value if value in {"open", "needs_review", "resolved", "dismissed"} else "open"


def _combined_review_status(records: list[dict]) -> str:
    statuses = {_review_status(record) for record in records if record}
    if "needs_review" in statuses:
        return "needs_review"
    if "open" in statuses or not statuses:
        return "open"
    if statuses == {"resolved"}:
        return "resolved"
    if statuses == {"dismissed"}:
        return "dismissed"
    return "open"


def _review_queue_id(kind: str, project: str | None, memory_ids: list[str], fingerprint: str = "") -> str:
    payload = {
        "kind": kind,
        "project": _canonical_project(project) if project else None,
        "memory_ids": sorted(str(item) for item in memory_ids),
        "fingerprint": fingerprint,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"review_{digest[:24]}"


def _detect_conflicts(request: MemoryDetectRequest) -> list[dict]:
    records = [
        item for item in _load_live_memories()
        if _memory_matches_filters(item, project=request.project, include_archived=request.include_archived)
    ]
    groups: dict[tuple[str, str], list[dict]] = {}
    for item in records:
        groups.setdefault((str(item.get("project") or ""), str(item.get("memory_type") or "")), []).append(item)

    candidates: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for (project, _memory_type), group in sorted(groups.items()):
        ordered = sorted(group, key=lambda item: str(item.get("source_id") or ""))
        title_buckets: dict[str, list[dict]] = {}
        prefix_tag_buckets: dict[tuple[str, str], list[dict]] = {}
        for item in ordered:
            metadata = item.get("metadata") or {}
            title = str(metadata.get("raw_title") or "").strip().lower()
            text = _normalized_text(item.get("content"))
            if title:
                title_buckets.setdefault(title, []).append(item)
            prefix = text[:80]
            if prefix:
                for tag in set(str(value) for value in (item.get("tags") or []) if str(value).strip()):
                    prefix_tag_buckets.setdefault((prefix, tag), []).append(item)

        candidate_pairs: set[tuple[str, str]] = set()
        for bucket in title_buckets.values():
            for left_index, left in enumerate(bucket):
                for right in bucket[left_index + 1:]:
                    candidate_pairs.add(tuple(sorted((str(left.get("source_id")), str(right.get("source_id"))))))
        for bucket in prefix_tag_buckets.values():
            for left_index, left in enumerate(bucket):
                for right in bucket[left_index + 1:]:
                    candidate_pairs.add(tuple(sorted((str(left.get("source_id")), str(right.get("source_id"))))))

        by_id = {str(item.get("source_id")): item for item in ordered}
        for left_id, right_id in sorted(candidate_pairs):
            if (left_id, right_id) in seen_pairs:
                continue
            seen_pairs.add((left_id, right_id))
            left = by_id[left_id]
            right = by_id[right_id]
            left_metadata = left.get("metadata") or {}
            right_metadata = right.get("metadata") or {}
            left_title = str(left_metadata.get("raw_title") or "").strip()
            right_title = str(right_metadata.get("raw_title") or "").strip()
            left_text = _normalized_text(left.get("content"))
            right_text = _normalized_text(right.get("content"))
            shared_tags = sorted(set(str(value) for value in (left.get("tags") or [])) & set(str(value) for value in (right.get("tags") or [])))
            same_title = bool(left_title and right_title and left_title.lower() == right_title.lower())
            same_prefix = bool(left_text[:80] and left_text[:80] == right_text[:80])
            materially_different = bool(left_text and right_text and left_text != right_text)

            score = 0.0
            reason = ""
            if same_title and materially_different:
                score = 0.9
                reason = "same_title_different_content"
            elif shared_tags and materially_different and same_prefix:
                score = 0.82
                reason = "shared_tags_divergent_content"
            if score <= 0:
                continue
            fingerprint = "|".join(
                [
                    reason,
                    _memory_content_sha256(left),
                    _memory_content_sha256(right),
                    ",".join(shared_tags),
                ]
            )
            candidates.append(
                {
                    "queue_id": _review_queue_id("contradiction", project, [left_id, right_id], fingerprint),
                    "left_id": left_id,
                    "right_id": right_id,
                    "project": project,
                    "score": score,
                    "reason": reason,
                    "left_title": left_title or _build_memory_title(left.get("content") or ""),
                    "right_title": right_title or _build_memory_title(right.get("content") or ""),
                    "shared_tags": shared_tags,
                    "left_revision_id": left_metadata.get("revision_id"),
                    "right_revision_id": right_metadata.get("revision_id"),
                    "left_content_sha256": _memory_content_sha256(left),
                    "right_content_sha256": _memory_content_sha256(right),
                }
            )
    candidates.sort(key=lambda item: (item.get("score") or 0.0, item.get("queue_id") or ""), reverse=True)
    return candidates[: max(min(request.limit, 200), 1)]


def _lint_memory_record(record: dict) -> list[dict]:
    issues: list[dict] = []
    content = record.get("content") or ""
    metadata = record.get("metadata") or {}
    if len(content.strip()) < 20:
        issues.append({"severity": "warning", "code": "too_short", "message": "memory content is very short"})
    if len(content) > 4000:
        issues.append({"severity": "warning", "code": "too_long", "message": "memory content is unusually long"})
    if not record.get("project"):
        issues.append({"severity": "error", "code": "missing_project", "message": "memory has no project scope"})
    if not record.get("memory_type"):
        issues.append({"severity": "error", "code": "missing_type", "message": "memory has no type"})
    if not (record.get("tags") or []):
        issues.append({"severity": "warning", "code": "missing_concepts", "message": "memory has no concepts/tags"})
    if not (metadata.get("files") or []):
        issues.append({"severity": "info", "code": "missing_files", "message": "memory has no linked files"})
    if contains_secret_like(content):
        issues.append({"severity": "error", "code": "possible_secret", "message": "memory may contain a secret-like token"})
    if content.count("\n") > 80:
        issues.append({"severity": "warning", "code": "log_like_blob", "message": "memory looks more like a raw log than compact durable knowledge"})
    return issues


def _lint_memory(request: MemoryLintRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")

    issues = _lint_memory_record(record)

    return {
        "memory": _serialize_memory_record(record),
        "issues": issues,
        "ok": not any(item["severity"] == "error" for item in issues),
    }


def _set_memory_confidence(request: MemoryConfidenceRequest) -> dict:
    if request.confidence < 0 or request.confidence > 1:
        raise HTTPException(status_code=400, detail="confidence must be between 0 and 1")
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    record.setdefault("metadata", {})["confidence"] = request.confidence
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _append_memory_changelog(record, "set_confidence", {"confidence": request.confidence})
    _replace_live_memory(record)
    return record


def _set_memory_pin(request: MemoryPinRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    record.setdefault("metadata", {})["pinned"] = request.pinned
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _append_memory_changelog(record, "set_pin", {"pinned": request.pinned})
    _replace_live_memory(record)
    return record


def _vote_memory_quality(request: MemoryVoteRequest) -> dict:
    if request.vote < 1 or request.vote > 5:
        raise HTTPException(status_code=400, detail="vote must be between 1 and 5")
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    votes = list(metadata.get("quality_votes") or [])
    votes.append({"voter": request.voter, "vote": request.vote})
    metadata["quality_votes"] = votes
    metadata["quality_score"] = round(sum(item["vote"] for item in votes) / len(votes), 3)
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _append_memory_changelog(record, "vote_quality", {"vote": request.vote, "voter": request.voter, "quality_score": metadata["quality_score"]})
    _replace_live_memory(record)
    return record


def _attach_source_refs(request: MemorySourceRefsRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    existing = list(metadata.get("source_refs") or [])
    metadata["source_refs"] = list(dict.fromkeys(existing + request.refs))
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _replace_live_memory(record)
    return record


def _get_source_refs(id: str, project: str | None = None) -> dict:
    record = _find_live_memory(id, project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.get("metadata") or {}
    return {"memory": _serialize_memory_record(record), "source_refs": metadata.get("source_refs") or []}


def _replace_source_refs(request: SourceRefsReplaceRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    metadata["source_refs"] = list(dict.fromkeys(request.refs))
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _replace_live_memory(record)
    return record


def _detach_source_refs(request: SourceRefsDetachRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    existing = list(metadata.get("source_refs") or [])
    metadata["source_refs"] = [item for item in existing if item not in set(request.refs)]
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _replace_live_memory(record)
    return record


def _restore_archived_memory(request: RestoreMemoryRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    metadata.pop("archived_at", None)
    metadata.pop("archive_reason", None)
    metadata["restored_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record["updated_at"] = metadata["restored_at"]
    _append_memory_changelog(record, "restore", {})
    _replace_live_memory(record)
    return record


def _list_pinned_memories(project: str | None = None, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    items = [
        item for item in _load_live_memories()
        if _memory_matches_filters(item, project=project, include_archived=False)
        and bool((item.get("metadata") or {}).get("pinned"))
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _adr_supersede(project: str, old_id: str, new_id: str) -> dict:
    adrs = _load_adrs()
    old_record = next((item for item in adrs if item.get("id") == old_id and item.get("project") == project), None)
    new_record = next((item for item in adrs if item.get("id") == new_id and item.get("project") == project), None)
    if old_record is None:
        raise HTTPException(status_code=404, detail="old ADR not found")
    if new_record is None:
        raise HTTPException(status_code=404, detail="new ADR not found")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    old_record["status"] = "superseded"
    old_record["updated_at"] = now
    old_record.setdefault("metadata", {})["superseded_by"] = new_id
    new_record.setdefault("metadata", {})["supersedes"] = old_id
    new_record["updated_at"] = now
    _save_adrs(adrs)
    if old_record.get("memory_id") and new_record.get("memory_id"):
        _create_memory_link(
            MemoryLinkRequest(
                source_id=new_record["memory_id"],
                target_id=old_record["memory_id"],
                relation="supersedes",
                project=project,
                metadata={"artifact": "adr"},
            )
        )
    return {"old": old_record, "new": new_record}


def _create_adr(request: AdrCreateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = request.title.strip()
    concepts = list(dict.fromkeys((request.concepts or []) + ["adr", "architecture", "bhm"]))
    files = request.files or []
    upsert_key = request.upsert_key or f"adr:{request.project}:{title.lower()}"
    content = "\n".join(
        [
            title,
            f"context: {request.context}",
            f"decision: {request.decision}",
            f"consequences: {request.consequences}",
            f"status: {request.status}",
        ]
    )
    action, memory_record = _upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="architecture",
            content=content,
            concepts=concepts,
            files=files,
        )
    )
    items = _load_adrs()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": title,
                    "context": request.context,
                    "decision": request.decision,
                    "consequences": request.consequences,
                    "status": request.status,
                    "files": files,
                    "concepts": concepts,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_adrs(items)
            return action, item
    record = {
        "id": f"adr_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": title,
        "context": request.context,
        "decision": request.decision,
        "consequences": request.consequences,
        "status": request.status,
        "files": files,
        "concepts": concepts,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_adrs(items)
    return action, record


def _list_adrs(project: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = _load_adrs()
    if project:
        items = [item for item in items if item.get("project") == project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _create_handoff(request: HandoffCreateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = request.title.strip()
    concepts = list(dict.fromkeys((request.concepts or []) + ["handoff", "bhm"]))
    files = request.files or []
    upsert_key = request.upsert_key or f"handoff:{request.project}:{title.lower()}"
    content = "\n".join(
        [
            title,
            f"current_state: {request.current_state}",
            f"decisions: {request.decisions}",
            f"validation: {request.validation}",
            f"next_agent_action: {request.next_agent_action}",
            f"next_owner_id: {request.next_owner_id}",
            f"handoff_sla_deadline: {request.handoff_sla_deadline}",
        ]
    )
    action, memory_record = _upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=concepts,
            files=files,
        )
    )
    items = _load_handoffs()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": title,
                    "current_state": request.current_state,
                    "decisions": request.decisions,
                    "validation": request.validation,
                    "next_agent_action": request.next_agent_action,
                    "next_owner_id": request.next_owner_id,
                    "handoff_sla_deadline": request.handoff_sla_deadline,
                    "files": files,
                    "concepts": concepts,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_handoffs(items)
            return action, item
    record = {
        "id": f"handoff_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": title,
        "current_state": request.current_state,
        "decisions": request.decisions,
        "validation": request.validation,
        "next_agent_action": request.next_agent_action,
        "next_owner_id": request.next_owner_id,
        "handoff_sla_deadline": request.handoff_sla_deadline,
        "files": files,
        "concepts": concepts,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_handoffs(items)
    return action, record


def _list_handoffs(project: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = _load_handoffs()
    if project:
        items = [item for item in items if item.get("project") == project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _session_upsert_key(request: SessionRecordCreateRequest, project: str, title: str) -> str:
    if request.upsert_key and request.upsert_key.strip():
        return request.upsert_key.strip()
    title_slug = re.sub(r"[^a-z0-9._-]+", "-", title.strip().lower()).strip("-")
    return f"session-record:{project}:{title_slug or 'session'}"


def _create_session_record(request: SessionRecordCreateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = request.title.strip()
    files_touched = request.files_touched or []
    concepts = ["hybrid-session", "checkpoint", "conversation", "bhm"]
    project = _canonical_project(request.project)
    upsert_key = _session_upsert_key(request, project, title)
    content = "\n".join(
        [
            f"{request.project} hybrid session record:",
            f"title: {title}",
            f"done: {request.done}",
            f"next: {request.next}",
            f"checks: {request.checks}",
            f"risks/notes: {request.risks}",
            f"decisions: {request.decisions}",
            f"files_touched: {', '.join(files_touched)}",
            f"conversation_notes: {request.conversation_notes}",
            f"transcript_ref: {request.transcript_ref}",
        ]
    )
    action, memory_record = _upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=project,
            type="workflow",
            content=content,
            concepts=concepts,
            files=files_touched,
        )
    )
    items = _load_session_records()
    for item in items:
        if item.get("project") in _project_aliases(project) and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": title,
                    "done": request.done,
                    "next": request.next,
                    "checks": request.checks,
                    "risks": request.risks,
                    "decisions": request.decisions,
                    "files_touched": files_touched,
                    "conversation_notes": request.conversation_notes,
                    "transcript_ref": request.transcript_ref,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            item.setdefault("metadata", {})["artifact_kind"] = "session-record"
            _save_session_records(items)
            return action, item
    record = {
        "id": f"session_bhm_{uuid.uuid4().hex[:16]}",
        "project": project,
        "title": title,
        "done": request.done,
        "next": request.next,
        "checks": request.checks,
        "risks": request.risks,
        "decisions": request.decisions,
        "files_touched": files_touched,
        "conversation_notes": request.conversation_notes,
        "transcript_ref": request.transcript_ref,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key, "artifact_kind": "session-record"},
    }
    items.append(record)
    _save_session_records(items)
    return action, record


def _list_session_records(project: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = _load_session_records()
    if project:
        items = [item for item in items if item.get("project") == project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _open_task_unlocked(request: TaskOpenRequest) -> tuple[str, dict]:
    project = _canonical_project(request.project)
    task_id = request.task_id.strip()
    intent = request.intent.strip()
    title = request.title.strip() or f"Task {task_id}"
    upsert_key = request.upsert_key or f"task:{project}:{task_id}"
    session_upsert_key = f"session-record:task:{project}:{task_id}"
    tasks = _load_tasks()
    existing = next(
        (
            item
            for item in tasks
            if item.get("project") == project
            and (
                item.get("task_id") == task_id
                or (item.get("metadata") or {}).get("upsert_key") == upsert_key
            )
        ),
        None,
    )

    requested_fields = {
        "project": project,
        "task_id": task_id,
        "title": title,
        "intent": intent,
        "scope_in": list(dict.fromkeys(request.scope_in)),
        "scope_out": list(dict.fromkeys(request.scope_out)),
        "repo": request.repo.strip(),
        "owner": request.owner.strip(),
        "session_id": request.session_id.strip(),
        "correlation_id": request.correlation_id.strip(),
        "files_touched": list(dict.fromkeys(request.files_touched)),
        "status": "open",
    }
    if existing is not None and all(
        existing.get(field) == value for field, value in requested_fields.items()
    ) and existing.get("session_record_id") and existing.get("memory_id"):
        return "already_open", existing

    now = _utc_now_iso()
    previous = existing or {}
    session_id = requested_fields["session_id"] or str(previous.get("session_id") or "")
    correlation_id = requested_fields["correlation_id"] or str(previous.get("correlation_id") or "")
    session_title = title
    session_notes = "\n".join(
        [
            f"task_id: {task_id}",
            f"intent: {intent}",
            f"scope_in: {', '.join(requested_fields['scope_in'])}",
            f"scope_out: {', '.join(requested_fields['scope_out'])}",
            f"repo: {requested_fields['repo']}",
            f"owner: {requested_fields['owner']}",
            f"session_id: {session_id}",
            f"correlation_id: {correlation_id}",
        ]
    )
    session_action, session_record = _create_session_record(
        SessionRecordCreateRequest(
            project=project,
            title=session_title,
            next=intent,
            checks="task_open",
            decisions=f"task_id: {task_id}",
            files_touched=requested_fields["files_touched"],
            conversation_notes=session_notes,
            transcript_ref=session_id,
            upsert_key=session_upsert_key,
        )
    )
    attributes = dict((previous.get("metadata") or {}).get("attributes") or {})
    attributes.update(request.metadata)
    record = {
        "id": previous.get("id") or f"task_bhm_{uuid.uuid4().hex[:16]}",
        **requested_fields,
        "session_id": session_id,
        "correlation_id": correlation_id,
        "session_record_id": session_record.get("id"),
        "memory_id": session_record.get("memory_id"),
        "opened_at": previous.get("opened_at") or now,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "metadata": {
            "upsert_key": upsert_key,
            "session_upsert_key": session_upsert_key,
            "attributes": attributes,
            "session_action": session_action,
        },
    }
    if existing is None:
        tasks.append(record)
        action = "created"
    else:
        index = tasks.index(existing)
        tasks[index] = record
        action = "reopened" if previous.get("status") == "closed" else "updated"
    _save_tasks(tasks)
    return action, record


def _open_task(request: TaskOpenRequest) -> tuple[str, dict]:
    with _TASK_LIFECYCLE_LOCK:
        return _open_task_unlocked(request)


def _close_task_unlocked(request: TaskCloseRequest) -> tuple[str, dict]:
    project = _canonical_project(request.project)
    task_id = request.task_id.strip()
    tasks = _load_tasks()
    index = next(
        (
            position
            for position, item in enumerate(tasks)
            if item.get("project") == project and item.get("task_id") == task_id
        ),
        None,
    )
    if index is None:
        raise HTTPException(status_code=404, detail="task not found")

    existing = tasks[index]
    files_touched = list(
        dict.fromkeys(
            request.files_touched
            if request.files_touched is not None
            else list(existing.get("files_touched") or [])
        )
    )
    requested_fields = {
        "done": request.done,
        "next": request.next,
        "checks": request.checks,
        "risks": request.risks,
        "decisions": request.decisions,
        "validation": request.validation,
        "files_touched": files_touched,
        "conversation_notes": request.conversation_notes,
        "transcript_ref": request.transcript_ref or str(existing.get("session_id") or ""),
    }
    if existing.get("status") == "closed" and all(
        existing.get(field) == value for field, value in requested_fields.items()
    ):
        return "already_closed", existing

    session_upsert_key = str(
        (existing.get("metadata") or {}).get("session_upsert_key")
        or f"session-record:task:{project}:{task_id}"
    )
    session_notes = "\n".join(
        [
            f"task_id: {task_id}",
            f"intent: {existing.get('intent') or ''}",
            f"validation: {request.validation}",
            request.conversation_notes,
        ]
    )
    session_action, session_record = _create_session_record(
        SessionRecordCreateRequest(
            project=project,
            title=str(existing.get("title") or f"Task {task_id}"),
            done=request.done,
            next=request.next,
            checks=request.checks,
            risks=request.risks,
            decisions=request.decisions,
            files_touched=files_touched,
            conversation_notes=session_notes,
            transcript_ref=requested_fields["transcript_ref"],
            upsert_key=session_upsert_key,
        )
    )

    now = _utc_now_iso()
    metadata = dict(existing.get("metadata") or {})
    attributes = dict(metadata.get("attributes") or {})
    attributes.update(request.metadata)
    metadata["attributes"] = attributes
    metadata["close_action"] = session_action
    record = {
        **existing,
        **requested_fields,
        "status": "closed",
        "closed_at": now,
        "updated_at": now,
        "session_record_id": session_record.get("id"),
        "memory_id": session_record.get("memory_id"),
        "metadata": metadata,
    }
    tasks[index] = record
    _save_tasks(tasks)
    return ("reclosed" if existing.get("status") == "closed" else "closed"), record


def _close_task(request: TaskCloseRequest) -> tuple[str, dict]:
    with _TASK_LIFECYCLE_LOCK:
        return _close_task_unlocked(request)


def _get_task(task_id: str, project: str | None = None) -> dict:
    project_name = _canonical_project(project) if project else None
    for item in _load_tasks():
        if item.get("task_id") != task_id:
            continue
        if project_name and item.get("project") != project_name:
            continue
        return item
    raise HTTPException(status_code=404, detail="task not found")


def _list_tasks(project: str | None, status: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    project_name = _canonical_project(project) if project else None
    items = [
        item
        for item in _load_tasks()
        if (not project_name or item.get("project") == project_name)
        and (not status or item.get("status") == status)
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _upsert_task_context(request: TaskContextUpdateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    upsert_key = request.upsert_key or f"task-context:{request.project}"
    files_touched = request.files_touched or []
    content = "\n".join(
        [
            f"{request.project} task context:",
            f"title: {request.title}",
            f"current_task: {request.current_task}",
            f"status: {request.status}",
            f"pending_items: {request.pending_items}",
            f"guidance: {request.guidance}",
            f"next_step: {request.next_step}",
            f"files_touched: {', '.join(files_touched)}",
        ]
    )
    action, memory_record = _upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=["task-context", "bhm"],
            files=files_touched,
        )
    )
    items = _load_task_contexts()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": request.title,
                    "current_task": request.current_task,
                    "status": request.status,
                    "pending_items": request.pending_items,
                    "guidance": request.guidance,
                    "next_step": request.next_step,
                    "files_touched": files_touched,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_task_contexts(items)
            return action, item
    record = {
        "id": f"task_context_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": request.title,
        "current_task": request.current_task,
        "status": request.status,
        "pending_items": request.pending_items,
        "guidance": request.guidance,
        "next_step": request.next_step,
        "files_touched": files_touched,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_task_contexts(items)
    return action, record


def _get_task_context(project: str) -> dict:
    items = _load_task_contexts()
    for item in items:
        if item.get("project") == project:
            return item
    raise HTTPException(status_code=404, detail="task context not found")


def _upsert_risk_register(request: RiskRegisterUpdateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    upsert_key = request.upsert_key or f"risk-register:{request.project}"
    top_risks = request.top_risks or []
    mitigations = request.mitigations or []
    content = "\n".join(
        [
            f"{request.project} risk register:",
            f"title: {request.title}",
            f"summary: {request.summary}",
            "top_risks:",
            *[f"- {item}" for item in top_risks],
            "mitigations:",
            *[f"- {item}" for item in mitigations],
            f"owner: {request.owner}",
        ]
    )
    action, memory_record = _upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=["risk-register", "bhm"],
            files=[],
        )
    )
    items = _load_risk_registers()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": request.title,
                    "summary": request.summary,
                    "top_risks": top_risks,
                    "mitigations": mitigations,
                    "owner": request.owner,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_risk_registers(items)
            return action, item
    record = {
        "id": f"risk_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": request.title,
        "summary": request.summary,
        "top_risks": top_risks,
        "mitigations": mitigations,
        "owner": request.owner,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_risk_registers(items)
    return action, record


def _get_risk_register(project: str) -> dict:
    items = _load_risk_registers()
    for item in items:
        if item.get("project") == project:
            return item
    raise HTTPException(status_code=404, detail="risk register not found")


def _save_validation_snapshot_record(request: ValidationSnapshotSaveRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    upsert_key = request.upsert_key or f"validation-snapshot:{request.project}"
    content = "\n".join(
        [
            f"{request.project} validation snapshot:",
            f"title: {request.title}",
            f"lint: {request.lint}",
            f"tests: {request.tests}",
            f"smoke: {request.smoke}",
            f"docs: {request.docs}",
            f"overall_status: {request.overall_status}",
            f"command_summary: {request.command_summary}",
        ]
    )
    action, memory_record = _upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=["validation-snapshot", "bhm"],
            files=[],
        )
    )
    items = _load_validation_snapshots()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": request.title,
                    "lint": request.lint,
                    "tests": request.tests,
                    "smoke": request.smoke,
                    "docs": request.docs,
                    "overall_status": request.overall_status,
                    "command_summary": request.command_summary,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_validation_snapshots(items)
            return action, item
    record = {
        "id": f"validation_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": request.title,
        "lint": request.lint,
        "tests": request.tests,
        "smoke": request.smoke,
        "docs": request.docs,
        "overall_status": request.overall_status,
        "command_summary": request.command_summary,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_validation_snapshots(items)
    return action, record


def _get_validation_snapshot(project: str) -> dict:
    items = _load_validation_snapshots()
    for item in items:
        if item.get("project") == project:
            return item
    raise HTTPException(status_code=404, detail="validation snapshot not found")


def _memory_timeline(request: MemoryTimelineRequest) -> list[dict]:
    items = [
        item for item in _load_live_memories()
        if _memory_matches_filters(
            item,
            project=request.project,
            memory_type=request.memory_type,
            concepts=[request.concept] if request.concept else None,
            include_archived=request.include_archived,
        )
    ]
    items.sort(key=lambda item: item.get("created_at") or item.get("updated_at") or "")
    return items[: max(min(request.limit, 200), 1)]


def _list_lessons(project: str, min_confidence: float = 0.0, limit: int = 10) -> list[dict]:
    lessons = [
        item for item in _load_lessons()
        if item.get("project") == project and float(item.get("confidence") or 0) >= min_confidence
    ]
    lessons.sort(
        key=lambda item: (
            float(item.get("confidence") or 0),
            item.get("created_at") or "",
        ),
        reverse=True,
    )
    return lessons[: max(min(limit, 200), 1)]


def _strengthen_lesson(request: LessonStrengthenRequest) -> dict:
    lessons = _load_lessons()
    for lesson in lessons:
        if lesson.get("id") != request.lessonId:
            continue
        if request.project and lesson.get("project") != request.project:
            continue
        current = float(lesson.get("confidence") or 0)
        lesson["confidence"] = min(round(current + 0.1, 3), 1.0)
        lesson["strengthened_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _save_lessons(lessons)
        return lesson
    raise HTTPException(status_code=404, detail="lesson not found")


def _build_reflection(project: str, max_clusters: int = 10) -> dict:
    concept_counts: dict[str, int] = {}
    memory_type_counts: dict[str, int] = {}
    for item in _load_live_memories():
        if not _memory_matches_filters(item, project=project, include_archived=False):
            continue
        memory_type = item.get("memory_type") or "unknown"
        memory_type_counts[memory_type] = memory_type_counts.get(memory_type, 0) + 1
        for concept in item.get("tags") or []:
            concept_counts[concept] = concept_counts.get(concept, 0) + 1
    clusters = [
        {"concept": concept, "count": count}
        for concept, count in sorted(concept_counts.items(), key=lambda pair: (-pair[1], pair[0]))[: max(max_clusters, 1)]
    ]
    memory_types = [
        {"type": memory_type, "count": count}
        for memory_type, count in sorted(memory_type_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
    ]
    return {
        "project": project,
        "clusters": clusters,
        "memory_types": memory_types,
        "memory_count": sum(memory_type_counts.values()),
    }


def _verify_memory(request: MemoryVerifyRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return {
        "verified": True,
        "memory": _serialize_memory_record(record),
    }


def _query_suggestions(project: str | None = None) -> list[str]:
    items = [
        item for item in _load_live_memories()
        if _memory_matches_filters(item, project=project, include_archived=False)
    ]
    tags: dict[str, int] = {}
    types: dict[str, int] = {}
    for item in items:
        for tag in item.get("tags") or []:
            tags[tag] = tags.get(tag, 0) + 1
        memory_type = item.get("memory_type")
        if memory_type:
            types[memory_type] = types.get(memory_type, 0) + 1
    suggestions = []
    for tag, _ in sorted(tags.items(), key=lambda pair: (-pair[1], pair[0]))[:5]:
        suggestions.append(f"concept:{tag}")
    for memory_type, _ in sorted(types.items(), key=lambda pair: (-pair[1], pair[0]))[:3]:
        suggestions.append(f"type:{memory_type}")
    suggestions.extend(
        [
            "latest checkpoint",
            "project map",
            "recent activity",
            "validation snapshot",
        ]
    )
    return list(dict.fromkeys(suggestions))[:12]


def _batch_upsert_memories(request: BatchUpsertMemoriesRequest) -> dict:
    results = []
    upserted_ids: dict[str, str] = {}
    for item in request.items:
        action, record = _upsert_live_memory(item)
        upserted_ids[item.upsert_key] = record["source_id"]
        results.append({"action": action, "memory": _serialize_memory_record(record)})
    return {"items": results, "count": len(results), "upserted_ids": upserted_ids}


def _batch_attach_source_refs(request: BatchAttachSourceRefsRequest) -> dict:
    results = []
    for item in request.items:
        record = _attach_source_refs(
            MemorySourceRefsRequest(
                id=item["id"],
                project=item.get("project"),
                refs=item.get("refs") or [],
            )
        )
        results.append(_serialize_memory_record(record))
    return {"memories": results, "count": len(results)}


def _integrity_audit(project: str | None = None) -> dict:
    memories = [
        item for item in _load_live_memories()
        if _memory_matches_filters(item, project=project, include_archived=True)
    ]
    memory_ids = {item.get("source_id") for item in memories}
    orphan_links = [
        item for item in _load_memory_links()
        if (not project or item.get("project") == project)
        and (item.get("source_id") not in memory_ids or item.get("target_id") not in memory_ids)
    ]
    duplicate_upsert_keys = []
    seen: dict[tuple[str, str], str] = {}
    for item in memories:
        upsert_key = (item.get("metadata") or {}).get("upsert_key")
        if not upsert_key:
            continue
        key = (item.get("project") or "", upsert_key)
        if key in seen:
            duplicate_upsert_keys.append({"project": key[0], "upsert_key": upsert_key, "ids": [seen[key], item.get("source_id")]})
        else:
            seen[key] = item.get("source_id")
    archived_but_pinned = [
        item.get("source_id") for item in memories
        if _is_archived_memory(item) and bool((item.get("metadata") or {}).get("pinned"))
    ]
    return {
        "project": project,
        "orphan_links": [_serialize_memory_link(item) for item in orphan_links],
        "duplicate_upsert_keys": duplicate_upsert_keys,
        "archived_but_pinned": archived_but_pinned,
        "ok": len(orphan_links) == 0 and len(duplicate_upsert_keys) == 0 and len(archived_but_pinned) == 0,
    }


def _repair_live_indexes(request: RepairLiveIndexesRequest) -> dict:
    report_before = _integrity_audit()
    removed_links = 0
    removed_artifacts = 0
    if request.remove_orphan_links:
        memories = _load_live_memories()
        memory_ids = {item.get("source_id") for item in memories}
        links = _load_memory_links()
        filtered = [
            item for item in links
            if item.get("source_id") in memory_ids and item.get("target_id") in memory_ids
        ]
        removed_links = len(links) - len(filtered)
        _save_memory_links(filtered)
    if request.remove_orphan_artifacts:
        memory_ids = {item.get("source_id") for item in _load_live_memories()}
        for loader, saver in (
            (_load_checkpoints, _save_checkpoints),
            (_load_project_maps, _save_project_maps),
            (_load_adrs, _save_adrs),
            (_load_handoffs, _save_handoffs),
            (_load_session_records, _save_session_records),
            (_load_task_contexts, _save_task_contexts),
            (_load_risk_registers, _save_risk_registers),
            (_load_validation_snapshots, _save_validation_snapshots),
        ):
            items = loader()
            filtered = [item for item in items if not item.get("memory_id") or item.get("memory_id") in memory_ids]
            removed_artifacts += len(items) - len(filtered)
            saver(filtered)
    report_after = _integrity_audit()
    return {"before": report_before, "after": report_after, "removed_links": removed_links, "removed_artifacts": removed_artifacts}


def _rebuild_project_summary(request: RebuildProjectSummaryRequest) -> dict:
    project_map = None
    try:
        project_map = _get_project_map(request.project)
    except HTTPException:
        pass
    latest_checkpoint = None
    try:
        latest_checkpoint = _get_latest_checkpoint(request.project)
    except HTTPException:
        pass
    task_context = None
    try:
        task_context = _get_task_context(request.project)
    except HTTPException:
        pass
    risk_register = None
    try:
        risk_register = _get_risk_register(request.project)
    except HTTPException:
        pass
    validation_snapshot = None
    try:
        validation_snapshot = _get_validation_snapshot(request.project)
    except HTTPException:
        pass
    lines = [f"{request.project} project summary:"]
    if project_map:
        lines.append(f"project_map: {project_map.get('title')}")
    if latest_checkpoint:
        lines.append(f"checkpoint: {latest_checkpoint.get('title')}")
    if task_context:
        lines.append(f"task_context: {task_context.get('current_task')}")
    if risk_register:
        lines.append(f"risk_summary: {risk_register.get('summary')}")
    if validation_snapshot:
        lines.append(f"validation: {validation_snapshot.get('overall_status')}")
    action, record = _upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=request.upsert_key or f"project-summary:{request.project}",
            project=request.project,
            type="architecture",
            content="\n".join(lines),
            concepts=["project-summary", "bhm"],
            files=[],
        )
    )
    return {"action": action, "memory": _serialize_memory_record(record)}


def _entity_extract(request: EntityExtractRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    content = record.get("content") or ""
    files = re.findall(r"\b[\w./-]+\.(?:py|ts|tsx|js|json|md|yml|yaml)\b", content)
    endpoints = re.findall(r"\b/(?:[A-Za-z0-9_.-]+/?)+", content)
    envs = re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", content)
    entities = {
        "files": sorted(set(files + ((record.get("metadata") or {}).get("files") or []))),
        "endpoints": sorted(set(endpoints)),
        "env_vars": sorted(set(envs)),
        "concepts": record.get("tags") or [],
    }
    return {"memory": _serialize_memory_record(record), "entities": entities}


def _relation_suggest(request: RelationSuggestRequest) -> dict:
    duplicates = _detect_duplicates(MemoryDetectRequest(project=request.project, limit=request.limit, include_archived=False))
    conflicts = _detect_conflicts(MemoryDetectRequest(project=request.project, limit=request.limit, include_archived=False))
    suggestions = []
    for item in duplicates:
        suggestions.append({"relation": "duplicate_of", "score": item["score"], "source_id": item["left_id"], "target_id": item["right_id"], "reason": item["reason"]})
    for item in conflicts:
        suggestions.append({"relation": "conflicts_with", "score": item["score"], "source_id": item["left_id"], "target_id": item["right_id"], "reason": item["reason"]})
    records = [
        item for item in _load_live_memories()
        if _memory_matches_filters(item, project=request.project, include_archived=False)
    ]
    for left_index, left in enumerate(records):
        for right in records[left_index + 1:]:
            left_files = set((left.get("metadata") or {}).get("files") or [])
            right_files = set((right.get("metadata") or {}).get("files") or [])
            shared_files = sorted(left_files & right_files)
            left_tags = set(left.get("tags") or [])
            right_tags = set(right.get("tags") or [])
            shared_tags = sorted(left_tags & right_tags)
            if shared_files:
                suggestions.append(
                    {
                        "relation": "relates_to",
                        "score": 0.7,
                        "source_id": left.get("source_id"),
                        "target_id": right.get("source_id"),
                        "reason": f"shared_files:{', '.join(shared_files[:3])}",
                    }
                )
            elif shared_tags:
                suggestions.append(
                    {
                        "relation": "relates_to",
                        "score": 0.6,
                        "source_id": left.get("source_id"),
                        "target_id": right.get("source_id"),
                        "reason": f"shared_concepts:{', '.join(shared_tags[:3])}",
                    }
                )
    suggestions.sort(key=lambda item: item["score"], reverse=True)
    return {"suggestions": suggestions[: max(min(request.limit, 200), 1)]}


def _compact_memory(request: MemoryCompactRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    metadata["compacted_from"] = record.get("content") or ""
    record["content"] = request.summary
    metadata["raw_title"] = _build_memory_title(request.summary)
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _append_memory_changelog(record, "compact", {"summary_chars": len(request.summary)})
    _replace_live_memory(record)
    return record


def _policy_guard(request: PolicyGuardRequest) -> dict:
    issues = []
    content = request.content or ""
    if contains_secret_like(content):
        issues.append({"severity": "error", "code": "possible_secret", "message": "content contains a secret-like token"})
    if len(content) > 8000:
        issues.append({"severity": "warning", "code": "too_large", "message": "content is too large for durable memory"})
    if content.count("\n") > 120:
        issues.append({"severity": "warning", "code": "raw_log_shape", "message": "content looks like a raw log dump"})
    if not request.project:
        issues.append({"severity": "warning", "code": "missing_project", "message": "project scope is missing"})
    if not request.memory_type:
        issues.append({"severity": "info", "code": "missing_type", "message": "memory type is missing"})
    return {"ok": not any(item["severity"] == "error" for item in issues), "issues": issues}


def _batch_archive_memories(request: BatchMemoryIdsRequest) -> dict:
    items = []
    for item in request.items:
        archived = _archive_live_memory(MemoryArchiveRequest(id=item["id"], project=item.get("project"), reason=item.get("reason", "batch_archive")))
        items.append(_serialize_memory_record(archived))
    return {"memories": items, "count": len(items)}


def _batch_delete_memories(request: BatchMemoryIdsRequest) -> dict:
    items = []
    for item in request.items:
        deleted = _delete_live_memory(MemoryDeleteRequest(id=item["id"], project=item.get("project")))
        items.append(_serialize_memory_record(deleted))
    return {"memories": items, "count": len(items)}


def _batch_link_memories(request: BatchLinkMemoriesRequest) -> dict:
    links = []
    for item in request.items:
        link = _create_memory_link(item)
        links.append(_serialize_memory_link(link))
    return {"links": links, "count": len(links)}


def _batch_unlink_memories(request: BatchMemoryIdsRequest) -> dict:
    deleted = []
    for item in request.items:
        deleted.append(
            _delete_memory_link(
                MemoryLinkDeleteRequest(
                    source_id=item["source_id"],
                    target_id=item["target_id"],
                    relation=item["relation"],
                    project=item["project"],
                )
            )
        )
    return {"count": len(deleted), "deleted": deleted}


def _memory_diff(request: MemoryDiffRequest) -> dict:
    left = _find_live_memory(request.left_id, request.project)
    right = _find_live_memory(request.right_id, request.project)
    if left is None or right is None:
        raise HTTPException(status_code=404, detail="memory not found")
    left_lines = (left.get("content") or "").splitlines()
    right_lines = (right.get("content") or "").splitlines()
    return {
        "left": _serialize_memory_record(left),
        "right": _serialize_memory_record(right),
        "same_content": (left.get("content") or "") == (right.get("content") or ""),
        "left_only": [line for line in left_lines if line not in right_lines],
        "right_only": [line for line in right_lines if line not in left_lines],
    }


def _project_summary_get(project: str) -> dict:
    record = _find_live_memory_by_upsert_key(project, f"project-summary:{project}")
    if record is None:
        raise HTTPException(status_code=404, detail="project summary not found")
    return _serialize_memory_record(record)


def _project_summary_pin(project: str) -> dict:
    record = _find_live_memory_by_upsert_key(project, f"project-summary:{project}")
    if record is None:
        raise HTTPException(status_code=404, detail="project summary not found")
    pinned = _set_memory_pin(MemoryPinRequest(id=record["source_id"], project=project, pinned=True))
    return pinned


def _project_summary_list(request: ProjectSummaryListRequest) -> tuple[list[dict], int]:
    items = [
        item for item in _load_live_memories()
        if str((item.get("metadata") or {}).get("upsert_key") or "").startswith("project-summary:")
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(request.offset, 0)
    end = start + max(min(request.limit, 200), 1)
    return items[start:end], total


def _artifact_integrity_audit(project: str | None = None) -> dict:
    memory_ids = {item.get("source_id") for item in _load_live_memories() if not project or item.get("project") == project}
    artifacts = {
        "checkpoints": [item for item in _load_checkpoints() if not project or item.get("project") == project],
        "project_maps": [item for item in _load_project_maps() if not project or item.get("project") == project],
        "adrs": [item for item in _load_adrs() if not project or item.get("project") == project],
        "handoffs": [item for item in _load_handoffs() if not project or item.get("project") == project],
        "session_records": [item for item in _load_session_records() if not project or item.get("project") == project],
        "tasks": [item for item in _load_tasks() if not project or item.get("project") == project],
        "task_contexts": [item for item in _load_task_contexts() if not project or item.get("project") == project],
        "risk_registers": [item for item in _load_risk_registers() if not project or item.get("project") == project],
        "validation_snapshots": [item for item in _load_validation_snapshots() if not project or item.get("project") == project],
    }
    orphans = {}
    for name, items in artifacts.items():
        missing = [item.get("id") for item in items if item.get("memory_id") and item.get("memory_id") not in memory_ids]
        if missing:
            orphans[name] = missing
    return {"project": project, "orphans": orphans, "ok": len(orphans) == 0}


def _link_graph_stats(project: str | None = None) -> dict:
    links = [item for item in _load_memory_links() if not project or item.get("project") == project]
    relation_counts = {}
    node_ids = set()
    for item in links:
        relation = item.get("relation") or "unknown"
        relation_counts[relation] = relation_counts.get(relation, 0) + 1
        node_ids.add(item.get("source_id"))
        node_ids.add(item.get("target_id"))
    return {"project": project, "link_count": len(links), "node_count": len(node_ids), "relation_counts": relation_counts}


def _reindex_memory_metadata(project: str | None = None) -> dict:
    live_records = _load_live_memories()
    updated = 0
    for item in live_records:
        if project and item.get("project") != project:
            continue
        metadata = item.setdefault("metadata", {})
        content = item.get("content") or ""
        raw_title = _build_memory_title(content)
        if metadata.get("raw_title") != raw_title:
            metadata["raw_title"] = raw_title
            updated += 1
    _save_live_memories(live_records)
    return {"project": project, "updated": updated}


def _memory_schema_validate(id: str, project: str | None = None) -> dict:
    record = _find_live_memory(id, project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    required = ["source_id", "project", "memory_type", "content", "created_at", "updated_at", "metadata"]
    missing = [field for field in required if field not in record or record.get(field) is None]
    return {"memory": _serialize_memory_record(record), "missing_fields": missing, "ok": len(missing) == 0}


def _memory_type_migrate(request: TypeMigrateRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    old_type = record.get("memory_type")
    record["memory_type"] = request.new_type
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _append_memory_changelog(record, "type_migrate", {"old_type": old_type, "new_type": request.new_type})
    _replace_live_memory(record)
    return record


def _search_hybrid(request: HybridSearchRequest) -> dict:
    advanced = _advanced_search_live_memories(
        MemoryAdvancedSearchRequest(
            query=request.query,
            project=request.project,
            domain=request.domain,
            semantic_type=request.semantic_type,
            priority=request.priority,
            include_archived=request.include_archived,
            include_logs=request.include_logs,
            limit=request.limit,
            offset=0,
        )
    )[0]
    suggestions = _query_suggestions(request.project)
    return {"memories": [_serialize_memory_record(item) for item in advanced], "suggestions": suggestions[:5]}


def _search_by_source_ref(request: SearchByRefRequest) -> dict:
    items = []
    for record in _load_live_memories():
        if not _memory_matches_filters(record, project=request.project, include_archived=False):
            continue
        refs = set(((record.get("metadata") or {}).get("source_refs") or []) + ((record.get("metadata") or {}).get("files") or []))
        if request.ref in refs:
            items.append(record)
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return {"memories": [_serialize_memory_record(item) for item in items[: max(min(request.limit, 200), 1)]], "ref": request.ref}


def _search_by_upsert_key(request: SearchByUpsertKeyRequest) -> dict:
    items = []
    for record in _load_live_memories():
        if request.project and record.get("project") != request.project:
            continue
        if ((record.get("metadata") or {}).get("upsert_key")) == request.upsert_key:
            items.append(record)
    return {"memories": [_serialize_memory_record(item) for item in items], "upsert_key": request.upsert_key}


def _list_archived_memories(project: str | None = None, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    items = [
        item for item in _load_live_memories()
        if _memory_matches_filters(item, project=project, include_archived=True) and _is_archived_memory(item)
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _artifact_store_pairs() -> dict[str, tuple[callable, callable]]:
    return {
        "checkpoint": (_load_checkpoints, _save_checkpoints),
        "project_map": (_load_project_maps, _save_project_maps),
        "adr": (_load_adrs, _save_adrs),
        "handoff": (_load_handoffs, _save_handoffs),
        "session_record": (_load_session_records, _save_session_records),
        "task": (_load_tasks, _save_tasks),
        "task_context": (_load_task_contexts, _save_task_contexts),
        "risk_register": (_load_risk_registers, _save_risk_registers),
        "validation_snapshot": (_load_validation_snapshots, _save_validation_snapshots),
    }


def _artifact_find(artifact_type: str, artifact_id: str, project: str | None = None) -> tuple[list[dict], callable, dict]:
    pairs = _artifact_store_pairs()
    if artifact_type not in pairs:
        raise HTTPException(status_code=400, detail="unsupported artifact type")
    loader, saver = pairs[artifact_type]
    items = loader()
    for item in items:
        if item.get("id") == artifact_id and (not project or item.get("project") == project):
            return items, saver, item
    raise HTTPException(status_code=404, detail="artifact not found")


def _record_age_days(record: dict) -> float:
    timestamp = record.get("updated_at") or record.get("created_at")
    if not timestamp:
        return 0.0
    return max((datetime.now(timezone.utc) - _safe_datetime(timestamp)).total_seconds() / 86400.0, 0.0)


def _batch_restore_memories(request: BatchRestoreRequest) -> dict:
    restored = []
    for item in request.items:
        record = _restore_archived_memory(RestoreMemoryRequest(id=item["id"], project=item.get("project")))
        restored.append(_serialize_memory_record(record))
    return {"memories": restored, "count": len(restored)}


def _artifact_restore(request: ArtifactRestoreRequest) -> dict:
    items, saver, artifact = _artifact_find(request.artifact_type, request.artifact_id, request.project)
    memory_id = artifact.get("memory_id")
    action = "none"
    restored_memory = None
    if memory_id:
        record = _find_live_memory(memory_id, request.project or artifact.get("project"))
        if record is not None and _is_archived_memory(record):
            restored_memory = _restore_archived_memory(
                RestoreMemoryRequest(id=memory_id, project=request.project or artifact.get("project"))
            )
            action = "restored_memory"
    else:
        content_parts = [artifact.get("title") or artifact.get("project") or request.artifact_type]
        for field in ("content", "summary", "decision", "current_task", "done", "overall_status"):
            value = artifact.get(field)
            if value:
                content_parts.append(str(value))
        upsert_action, memory = _upsert_live_memory(
            MemoryUpsertRequest(
                upsert_key=f"{request.artifact_type}:{artifact.get('project')}:{artifact.get('id')}",
                project=artifact.get("project") or request.project or "e-github-workspace",
                type="workflow",
                content="\n".join(content_parts),
                concepts=[request.artifact_type, "artifact-restore", "bhm"],
                files=artifact.get("files") or artifact.get("files_touched") or [],
            )
        )
        artifact["memory_id"] = memory.get("source_id")
        artifact["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        saver(items)
        action = upsert_action
        restored_memory = memory
    return {
        "artifact_type": request.artifact_type,
        "artifact": artifact,
        "action": action,
        "memory": _serialize_memory_record(restored_memory) if restored_memory else None,
    }


def _orphan_artifact_relink(request: OrphanArtifactRelinkRequest) -> dict:
    items, saver, artifact = _artifact_find(request.artifact_type, request.artifact_id, request.project)
    target = _find_live_memory(request.target_memory_id, request.project or artifact.get("project"))
    if target is None:
        raise HTTPException(status_code=404, detail="target memory not found")
    artifact["memory_id"] = request.target_memory_id
    artifact["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    saver(items)
    return {"artifact_type": request.artifact_type, "artifact": artifact, "memory": _serialize_memory_record(target)}


def _memory_staleness_report(request: MemoryStalenessReportRequest) -> dict:
    stale = []
    for item in _load_live_memories():
        if not _memory_matches_filters(item, project=request.project, include_archived=False):
            continue
        age_days = _record_age_days(item)
        if age_days >= request.days:
            stale.append({"age_days": round(age_days, 1), "memory": _serialize_memory_record(item)})
    stale.sort(key=lambda item: item["age_days"], reverse=True)
    return {"project": request.project, "days": request.days, "items": stale[: max(min(request.limit, 200), 1)]}


def _memory_review_queue(request: MemoryReviewQueueRequest) -> dict:
    records = [
        item for item in _load_live_memories()
        if _memory_matches_filters(item, project=request.project, include_archived=False)
    ]
    by_id = {str(item.get("source_id")): item for item in records}
    queue: list[dict] = []
    for item in records:
        issues = _lint_memory_record(item)
        confidence = (item.get("metadata") or {}).get("confidence")
        reasons = [issue["code"] for issue in issues if issue["severity"] in {"error", "warning"}]
        if confidence is not None and confidence < 0.5:
            reasons.append("low_confidence")
        if not ((item.get("metadata") or {}).get("source_refs") or []):
            reasons.append("missing_source_refs")
        if not reasons:
            continue
        status = _review_status(item)
        if status in {"resolved", "dismissed"} and not request.include_closed:
            continue
        queue.append(
            {
                "queue_id": _review_queue_id("quality", item.get("project"), [item["source_id"]], "|".join(sorted(set(reasons))) + "|" + _memory_content_sha256(item)),
                "kind": "quality",
                "status": status,
                "score": min(1.0, 0.35 + 0.15 * len(set(reasons))),
                "reasons": sorted(set(reasons)),
                "memory_ids": [item["source_id"]],
                "memory": _serialize_memory_record(item),
            }
        )

    if request.include_conflicts:
        conflicts = _detect_conflicts(
            MemoryDetectRequest(project=request.project, limit=200, include_archived=False)
        )
        for conflict in conflicts:
            left = by_id.get(conflict["left_id"])
            right = by_id.get(conflict["right_id"])
            status = _combined_review_status([left, right])
            if status in {"resolved", "dismissed"} and not request.include_closed:
                continue
            queue.append(
                {
                    "queue_id": conflict["queue_id"],
                    "kind": "contradiction",
                    "status": status,
                    "score": conflict["score"],
                    "reasons": [conflict["reason"]],
                    "memory_ids": [conflict["left_id"], conflict["right_id"]],
                    "payload": _serialize_conflict_candidate(conflict),
                }
            )
    queue.sort(key=lambda item: (item.get("score") or 0.0, item.get("queue_id") or ""), reverse=True)
    bounded_queue = queue[: max(min(request.limit, 200), 1)]
    return {
        "project": request.project,
        "items": bounded_queue,
        "lifecycle_suggestions": build_lifecycle_suggestions(bounded_queue),
    }


def _memory_triage_queue(request: MemoryTriageQueueRequest) -> dict:
    records = [
        item for item in _load_live_memories()
        if _memory_matches_filters(item, project=request.project, include_archived=False)
    ]
    by_id = {str(item.get("source_id")): item for item in records}
    duplicates = _detect_duplicates(MemoryDetectRequest(project=request.project, limit=200, include_archived=False))
    conflicts = _detect_conflicts(MemoryDetectRequest(project=request.project, limit=200, include_archived=False))
    suggestions = _relation_suggest(RelationSuggestRequest(project=request.project, limit=200)).get("suggestions", [])
    queue: list[dict] = []
    seen_queue_ids: set[str] = set()
    for item in duplicates:
        source_id, target_id = item["left_id"], item["right_id"]
        status = _combined_review_status([by_id.get(source_id), by_id.get(target_id)])
        if status in {"resolved", "dismissed"} and not request.include_closed:
            continue
        queue_id = _review_queue_id("duplicate", item.get("project"), [source_id, target_id], f"{item['reason']}|{item['score']}")
        if queue_id in seen_queue_ids:
            continue
        seen_queue_ids.add(queue_id)
        queue.append({"queue_id": queue_id, "kind": "duplicate", "status": status, "score": item["score"], "memory_ids": [source_id, target_id], "payload": item})
    for item in conflicts:
        status = _combined_review_status([by_id.get(item["left_id"]), by_id.get(item["right_id"])])
        if status in {"resolved", "dismissed"} and not request.include_closed:
            continue
        queue_id = item["queue_id"]
        if queue_id in seen_queue_ids:
            continue
        seen_queue_ids.add(queue_id)
        queue.append({"queue_id": queue_id, "kind": "conflict", "status": status, "score": item["score"], "memory_ids": [item["left_id"], item["right_id"]], "payload": item})
    for item in suggestions:
        source_id, target_id = item["source_id"], item["target_id"]
        status = _combined_review_status([by_id.get(source_id), by_id.get(target_id)])
        if status in {"resolved", "dismissed"} and not request.include_closed:
            continue
        queue_id = _review_queue_id(item["relation"], request.project, [source_id, target_id], f"{item['reason']}|{item['score']}")
        if queue_id in seen_queue_ids:
            continue
        seen_queue_ids.add(queue_id)
        queue.append({"queue_id": queue_id, "kind": "relation_suggestion", "status": status, "score": item["score"], "memory_ids": [source_id, target_id], "payload": item})
    queue.sort(key=lambda item: (item.get("score") or 0.0, item.get("queue_id") or ""), reverse=True)
    bounded_queue = queue[: max(min(request.limit, 200), 1)]
    return {
        "project": request.project,
        "items": bounded_queue,
        "lifecycle_suggestions": build_lifecycle_suggestions(bounded_queue),
    }


def _project_summary_refresh_all(request: ProjectSummaryRefreshAllRequest) -> dict:
    projects = request.projects or sorted({item.get("project") for item in _load_live_memories() if item.get("project")})
    results = []
    for project in projects:
        results.append(_rebuild_project_summary(RebuildProjectSummaryRequest(project=project)))
    return {"projects": projects, "count": len(results), "items": results}


def _relation_apply_suggestions(request: RelationApplySuggestionsRequest) -> dict:
    suggestions = _relation_suggest(RelationSuggestRequest(project=request.project, limit=request.limit)).get("suggestions", [])
    created = []
    for item in suggestions:
        if item["score"] < request.min_score:
            continue
        if item["relation"] == "relates_to" and not request.include_relates_to:
            continue
        link = _create_memory_link(
            MemoryLinkRequest(
                source_id=item["source_id"],
                target_id=item["target_id"],
                relation=item["relation"],
                project=request.project or _find_live_memory(item["source_id"]).get("project"),
                metadata={"suggested": True, "score": item["score"], "reason": item["reason"]},
            )
        )
        created.append(_serialize_memory_link(link))
    return {"project": request.project, "links": created, "count": len(created)}


def _memory_merge_preview(request: MemoryMergePreviewRequest) -> dict:
    source = _find_live_memory(request.source_id, request.project)
    target = _find_live_memory(request.target_id, request.project)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="memory not found")
    merged_tags = sorted(set((source.get("tags") or []) + (target.get("tags") or [])))
    merged_files = sorted(set(((source.get("metadata") or {}).get("files") or []) + ((target.get("metadata") or {}).get("files") or [])))
    merged_content = (target.get("content") or "").strip()
    if (source.get("content") or "").strip() and (source.get("content") or "").strip() not in merged_content:
        merged_content = f"{merged_content}\n\n[source supplement]\n{source.get('content')}".strip()
    return {
        "source": _serialize_memory_record(source),
        "target": _serialize_memory_record(target),
        "preview": {"tags": merged_tags, "files": merged_files, "content": merged_content},
    }


def _schema_upgrade_all(request: SchemaUpgradeAllRequest) -> dict:
    live_records = _load_live_memories()
    upgraded = 0
    for item in live_records:
        if request.project and item.get("project") != request.project:
            continue
        changed = False
        metadata = item.setdefault("metadata", {})
        if "files" not in metadata:
            metadata["files"] = []
            changed = True
        if "source_refs" not in metadata:
            metadata["source_refs"] = []
            changed = True
        if "raw_title" not in metadata:
            metadata["raw_title"] = _build_memory_title(item.get("content") or "")
            changed = True
        if "tags" in item and item.get("tags") is None:
            item["tags"] = []
            changed = True
        if changed:
            upgraded += 1
    _save_live_memories(live_records)
    return {"project": request.project, "upgraded": upgraded}


def _memory_redact(request: MemoryRedactRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    original = record.get("content") or ""
    redaction_kinds: list[str] = []
    if request.patterns:
        if len(original) > _CUSTOM_REDACTION_MAX_INPUT_CHARS:
            raise HTTPException(status_code=413, detail="custom redaction input exceeds the bounded limit")
        redacted = original
        replacements = 0
        for pattern in request.patterns:
            try:
                compiled = compile_bounded_regex(
                    pattern,
                    field="redaction pattern",
                    max_length=_CUSTOM_REDACTION_MAX_PATTERN_LENGTH,
                )
            except SecurityBoundaryError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            redacted, count = compiled.subn(
                lambda match: ((match.group(1) if match.lastindex else "") + request.replacement),
                redacted,
            )
            replacements += count
        if replacements:
            redaction_kinds.append("custom")
    else:
        redaction = redact_secret_text(original)
        redacted = re.sub(r"\[REDACTED:[^\]]+\]", request.replacement, redaction.value)
        replacements = redaction.replacements
        redaction_kinds.extend(sorted(set(redaction.kinds)))
    metadata = record.setdefault("metadata", {})
    removed_legacy_plaintext = metadata.pop("content_before_redaction", None) is not None
    if replacements or removed_legacy_plaintext:
        metadata["redacted_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        metadata["redaction_count"] = replacements
        metadata["redaction_kinds"] = redaction_kinds
        metadata["content_before_redaction_sha256"] = hashlib.sha256(original.encode("utf-8")).hexdigest()
        metadata["content_before_redaction_chars"] = len(original)
        record["content"] = redacted
        record["updated_at"] = metadata["redacted_at"]
        _append_memory_changelog(
            record,
            "redact",
            {
                "replacements": replacements,
                "kinds": redaction_kinds,
                "removed_legacy_plaintext": removed_legacy_plaintext,
            },
        )
        _replace_live_memory(record)
    return {"memory": _serialize_memory_record(record), "replacements": replacements}


def _secret_scan_existing_memories(request: SecretScanRequest) -> dict:
    findings = []
    for item in _load_live_memories():
        if not _memory_matches_filters(item, project=request.project, include_archived=True):
            continue
        redaction = redact_secret_text(item.get("content") or "")
        if redaction.replacements:
            findings.append(
                {
                    "memory": _serialize_memory_record(item),
                    "match_count": redaction.replacements,
                    "keywords": sorted(set(redaction.kinds)),
                }
            )
    findings.sort(key=lambda item: item["match_count"], reverse=True)
    return {"project": request.project, "findings": findings[: max(min(request.limit, 500), 1)]}


def _agent_activity_rollup(project: str | None = None) -> dict:
    artifacts = {
        "checkpoints": [item for item in _load_checkpoints() if not project or item.get("project") == project],
        "handoffs": [item for item in _load_handoffs() if not project or item.get("project") == project],
        "tasks": [item for item in _load_tasks() if not project or item.get("project") == project],
        "session_records": [item for item in _load_session_records() if not project or item.get("project") == project],
        "observations": [item for item in _load_observations() if not project or item.get("project") == project],
    }
    return {
        "project": project,
        "counts": {name: len(items) for name, items in artifacts.items()},
        "latest": {
            name: max((item.get("updated_at") or item.get("created_at") or "" for item in items), default="")
            for name, items in artifacts.items()
        },
    }


def _project_memory_heatmap(project: str | None = None) -> dict:
    records = [item for item in _load_live_memories() if _memory_matches_filters(item, project=project, include_archived=False)]
    type_counts = Counter(item.get("memory_type") or "unknown" for item in records)
    tag_counts = Counter(tag for item in records for tag in (item.get("tags") or []))
    age_buckets = {"0_7": 0, "8_30": 0, "31_90": 0, "91_plus": 0}
    for item in records:
        age = _record_age_days(item)
        if age <= 7:
            age_buckets["0_7"] += 1
        elif age <= 30:
            age_buckets["8_30"] += 1
        elif age <= 90:
            age_buckets["31_90"] += 1
        else:
            age_buckets["91_plus"] += 1
    return {"project": project, "type_counts": dict(type_counts), "top_tags": dict(tag_counts.most_common(10)), "age_buckets": age_buckets}


def _relation_confidence_set(request: RelationConfidenceRequest) -> dict:
    links = _load_memory_links()
    for item in links:
        if item.get("source_id") == request.source_id and item.get("target_id") == request.target_id and item.get("relation") == request.relation and item.get("project") == request.project:
            metadata = item.setdefault("metadata", {})
            metadata["confidence"] = request.confidence
            metadata["confidence_updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            _save_memory_links(links)
            return _serialize_memory_link(item)
    raise HTTPException(status_code=404, detail="relation not found")


def _relation_vote_quality(request: RelationVoteRequest) -> dict:
    links = _load_memory_links()
    for item in links:
        if item.get("source_id") == request.source_id and item.get("target_id") == request.target_id and item.get("relation") == request.relation and item.get("project") == request.project:
            metadata = item.setdefault("metadata", {})
            votes = metadata.setdefault("quality_votes", [])
            votes.append({"vote": request.vote, "voter": request.voter, "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
            metadata["quality_score"] = round(sum(v["vote"] for v in votes) / len(votes), 3)
            _save_memory_links(links)
            return _serialize_memory_link(item)
    raise HTTPException(status_code=404, detail="relation not found")


def _memory_alias_add(request: MemoryAliasRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    aliases = (record.setdefault("metadata", {})).setdefault("aliases", [])
    if request.alias not in aliases:
        aliases.append(request.alias)
        record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _append_memory_changelog(record, "alias_add", {"alias": request.alias})
        _replace_live_memory(record)
    return record


def _memory_alias_remove(request: MemoryAliasRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    aliases = (record.setdefault("metadata", {})).setdefault("aliases", [])
    record["metadata"]["aliases"] = [item for item in aliases if item != request.alias]
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _append_memory_changelog(record, "alias_remove", {"alias": request.alias})
    _replace_live_memory(record)
    return record


def _alias_resolve(request: AliasResolveRequest) -> dict:
    matches = []
    accepted_projects = _project_aliases(request.project)
    for item in _load_live_memories():
        if request.project and item.get("project") not in accepted_projects:
            continue
        aliases = (item.get("metadata") or {}).get("aliases") or []
        if request.alias in aliases:
            matches.append(_serialize_memory_record(item))
    return {"alias": request.alias, "memories": matches}


def _entity_catalog_rebuild(project: str | None = None) -> dict:
    records = [item for item in _load_live_memories() if _memory_matches_filters(item, project=project, include_archived=False)]
    files = Counter()
    endpoints = Counter()
    env_vars = Counter()
    concepts = Counter()
    for item in records:
        content = item.get("content") or ""
        for match in re.findall(r"\b[\w./-]+\.(?:py|ts|tsx|js|json|md|yml|yaml)\b", content):
            files[match] += 1
        for match in re.findall(r"\b/(?:[A-Za-z0-9_.-]+/?)+", content):
            endpoints[match] += 1
        for match in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", content):
            env_vars[match] += 1
        for concept in item.get("tags") or []:
            concepts[concept] += 1
    catalog = {
        "id": f"entity_catalog_{project or 'all'}",
        "project": project,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": dict(files.most_common(50)),
        "endpoints": dict(endpoints.most_common(50)),
        "env_vars": dict(env_vars.most_common(50)),
        "concepts": dict(concepts.most_common(50)),
    }
    catalogs = [item for item in _load_entity_catalogs() if item.get("project") != project]
    catalogs.append(catalog)
    _save_entity_catalogs(catalogs)
    return catalog


def _entity_catalog_get(project: str | None = None) -> dict:
    for item in _load_entity_catalogs():
        if item.get("project") == project:
            return item
    return _entity_catalog_rebuild(project)


def _project_summary_compare(request: ProjectSummaryCompareRequest) -> dict:
    left = _project_summary_get(request.left_project)
    right = _project_summary_get(request.right_project)
    left_lines = set((left.get("content") or "").splitlines())
    right_lines = set((right.get("content") or "").splitlines())
    return {
        "left_project": request.left_project,
        "right_project": request.right_project,
        "left_only": sorted(left_lines - right_lines),
        "right_only": sorted(right_lines - left_lines),
        "shared": sorted(left_lines & right_lines),
    }


def _memory_usage_stats(project: str | None = None) -> dict:
    records = [item for item in _load_live_memories() if _memory_matches_filters(item, project=project, include_archived=True)]
    archived = sum(1 for item in records if _is_archived_memory(item))
    pinned = sum(1 for item in records if (item.get("metadata") or {}).get("pinned"))
    content_chars = sum(len(item.get("content") or "") for item in records)
    refs = sum(len((item.get("metadata") or {}).get("source_refs") or []) for item in records)
    links = len([item for item in _load_memory_links() if not project or item.get("project") == project])
    return {
        "project": project,
        "memory_count": len(records),
        "archived_count": archived,
        "pinned_count": pinned,
        "content_chars": content_chars,
        "source_ref_count": refs,
        "link_count": links,
    }


def _forget_selector_values(request: ForgetPreviewRequest) -> tuple[str | None, list[str], list[str]]:
    project = _canonical_project(request.project) if request.project else None
    memory_ids = sorted({str(value).strip() for value in request.memory_ids if str(value).strip()})
    upsert_keys = sorted({str(value).strip() for value in request.upsert_keys if str(value).strip()})
    if not memory_ids and not upsert_keys:
        raise HTTPException(status_code=400, detail="forget preview requires memory_ids or upsert_keys")
    return project, memory_ids, upsert_keys


def _forget_preview_plan(request: ForgetPreviewRequest) -> dict:
    project, memory_ids, upsert_keys = _forget_selector_values(request)
    accepted_projects = _project_aliases(project)
    selected: list[dict] = []
    all_selected_ids: set[str] = set()
    for item in _load_live_memories():
        source_id = str(item.get("source_id") or "").strip()
        metadata = item.get("metadata") or {}
        if not source_id or (project and item.get("project") not in accepted_projects):
            continue
        id_match = not memory_ids or source_id in memory_ids
        key_match = not upsert_keys or str(metadata.get("upsert_key") or "") in upsert_keys
        if not (id_match and key_match):
            continue
        all_selected_ids.add(source_id)
        content = str(item.get("content") or "")
        content_sha256 = metadata.get("content_sha256") or hashlib.sha256(content.encode("utf-8")).hexdigest()
        selected.append(
            {
                "id": source_id,
                "project": item.get("project"),
                "title": metadata.get("raw_title") or _build_memory_title(item.get("content") or ""),
                "lifecycle": _memory_lifecycle(item),
                "memory_type": item.get("memory_type"),
                "upsert_key": metadata.get("upsert_key"),
                "revision_id": metadata.get("revision_id"),
                "content_sha256": content_sha256,
            }
        )
    selected.sort(key=lambda item: item["id"])
    missing_ids = sorted(set(memory_ids) - all_selected_ids)
    candidate_ids = sorted(all_selected_ids)
    digest_payload = {
        "project": project,
        "memory_ids": memory_ids,
        "upsert_keys": upsert_keys,
        "candidate_ids": candidate_ids,
        "candidate_fingerprints": [
            {
                "id": item["id"],
                "project": item.get("project"),
                "upsert_key": item.get("upsert_key"),
                "content_sha256": item.get("content_sha256"),
            }
            for item in selected
        ],
        "operation": request.operation,
        "reason": request.reason,
        "undo_window_seconds": request.undo_window_seconds,
    }
    plan_digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "project": project,
        "operation": request.operation,
        "reason": request.reason,
        "undo_window_seconds": request.undo_window_seconds,
        "memory_ids": memory_ids,
        "upsert_keys": upsert_keys,
        "candidate_count": len(selected),
        "candidates": selected[: request.limit],
        "candidate_ids": candidate_ids,
        "missing_ids": missing_ids,
        "truncated": len(selected) > request.limit,
        "plan_digest": plan_digest,
        "read_only": True,
    }


def _forget_preview(request: ForgetPreviewRequest) -> dict:
    return {"success": True, "mode": "preview", **_forget_preview_plan(request)}


def _tombstone_live_memory(memory_id: str, project: str | None, reason: str) -> dict:
    existing = _find_live_memory(memory_id, project)
    if existing is None:
        raise HTTPException(status_code=404, detail="memory not found")
    if _memory_lifecycle(existing) == "tombstoned":
        return existing
    try:
        deleted = _memory_service().tombstone(memory_id, reason=reason)
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc
    if deleted is None:
        raise HTTPException(status_code=409, detail="memory was already tombstoned")
    return deleted


def _restore_tombstoned_memory(memory_id: str, project: str | None, reason: str, undo_window_seconds: int) -> dict:
    existing = _find_live_memory(memory_id, project)
    if existing is None:
        raise HTTPException(status_code=404, detail="memory not found")
    if _memory_lifecycle(existing) != "tombstoned":
        return existing
    try:
        restored = _memory_service().restore_tombstone(
            memory_id,
            reason=reason,
            undo_window_seconds=undo_window_seconds,
        )
    except (InvalidTombstone, UndoWindowExpired) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc
    if restored is None:
        raise HTTPException(status_code=409, detail="memory is not tombstoned")
    return restored


def _forget_apply(request: ForgetApplyRequest) -> dict:
    if not request.confirm:
        raise HTTPException(status_code=400, detail="forget apply requires confirm=true")
    plan = _forget_preview_plan(request)
    project = plan["project"]
    if plan["missing_ids"] or plan["truncated"]:
        raise HTTPException(status_code=409, detail={"code": "forget_plan_changed", "plan": plan})
    if plan["plan_digest"] != request.preview_digest:
        raise HTTPException(status_code=409, detail={"code": "forget_preview_digest_mismatch", "plan": plan})
    results: list[dict] = []
    for candidate in plan["candidates"]:
        memory_id = candidate["id"]
        current = _find_live_memory(memory_id, project)
        if current is None:
            raise HTTPException(status_code=409, detail=f"memory disappeared before forget apply: {memory_id}")
        if request.operation == "tombstone":
            already = _memory_lifecycle(current) == "tombstoned"
            record = _tombstone_live_memory(memory_id, project, request.reason)
            action = "already_tombstoned" if already else "tombstoned"
        else:
            already = _memory_lifecycle(current) != "tombstoned"
            record = _restore_tombstoned_memory(
                memory_id,
                project,
                request.reason,
                request.undo_window_seconds,
            )
            action = "already_restored" if already else "restored"
        results.append({"id": memory_id, "action": action, "memory": _serialize_memory_record(record)})
    return {
        "success": True,
        "mode": "apply",
        "operation": request.operation,
        "plan_digest": plan["plan_digest"],
        "count": len(results),
        "results": results,
    }


def _recent_failures_feed(request: RecentFailuresFeedRequest) -> dict:
    failures = []
    for item in _load_validation_snapshots():
        if request.project and item.get("project") != request.project:
            continue
        if (item.get("overall_status") or "").lower() in {"failed", "error", "red"}:
            failures.append({"kind": "validation_snapshot", "artifact": _serialize_validation_snapshot_record(item)})
    for item in _load_handoffs():
        if request.project and item.get("project") != request.project:
            continue
        text = " ".join([item.get("current_state") or "", item.get("validation") or ""]).lower()
        if any(token in text for token in ("fail", "error", "blocked")):
            failures.append({"kind": "handoff", "artifact": _serialize_handoff_record(item)})
    failures.sort(key=lambda item: item["artifact"].get("updated_at") or item["artifact"].get("created_at") or "", reverse=True)
    return {"project": request.project, "items": failures[: max(min(request.limit, 200), 1)]}


def _memory_restore_hard_deleted_preview(request: HardDeleteRestorePreviewRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    project = request.project or record.get("project")
    artifact_refs = []
    for artifact_type, (loader, _) in _artifact_store_pairs().items():
        for item in loader():
            if item.get("project") != project:
                continue
            if item.get("memory_id") == request.id:
                artifact_refs.append({"artifact_type": artifact_type, "artifact_id": item.get("id"), "title": item.get("title")})
    links = [item for item in _load_memory_links() if item.get("source_id") == request.id or item.get("target_id") == request.id]
    preview = {
        "memory": _serialize_memory_record(record),
        "artifact_dependencies": artifact_refs,
        "link_count": len(links),
        "restorable_after_hard_delete": len(artifact_refs) > 0,
        "reconstruction_sources": ["artifact stores"] if artifact_refs else [],
        "warnings": [
            "hard delete removes dependent canonical artifact refs"
            if artifact_refs else "without surviving artifacts, reconstruction will be weak or impossible"
        ],
    }
    return preview


def _artifact_delete(request: ArtifactDeleteRequest) -> dict:
    items, saver, artifact = _artifact_find(request.artifact_type, request.artifact_id, request.project)
    remaining = [item for item in items if item.get("id") != request.artifact_id]
    saver(remaining)
    deleted_memory = None
    if request.delete_backing_memory and artifact.get("memory_id"):
        deleted_memory = _delete_live_memory_hard(HardDeleteMemoryRequest(id=artifact["memory_id"], project=request.project or artifact.get("project")))
    return {
        "artifact_type": request.artifact_type,
        "deleted": True,
        "artifact_id": request.artifact_id,
        "backing_memory_deleted": bool(deleted_memory),
        "backing_memory": _serialize_memory_record(deleted_memory) if deleted_memory else None,
    }


def _artifact_list_by_type(request: ArtifactListRequest) -> dict:
    loader, _ = _artifact_store_pairs().get(request.artifact_type) or (None, None)
    if loader is None:
        raise HTTPException(status_code=400, detail="unsupported artifact type")
    items = loader()
    if request.project:
        items = [item for item in items if item.get("project") == request.project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(request.offset, 0)
    end = start + max(min(request.limit, 200), 1)
    return {"artifact_type": request.artifact_type, "items": items[start:end], "total": total, "limit": max(min(request.limit, 200), 1), "offset": start}


def _artifact_usage_stats(project: str | None = None) -> dict:
    memory_ids = {item.get("source_id") for item in _load_live_memories() if not project or item.get("project") == project}
    counts = {}
    referenced_memory_ids = set()
    orphan_counts = {}
    for artifact_type, (loader, _) in _artifact_store_pairs().items():
        items = [item for item in loader() if not project or item.get("project") == project]
        counts[artifact_type] = len(items)
        orphan_counts[artifact_type] = sum(1 for item in items if item.get("memory_id") and item.get("memory_id") not in memory_ids)
        referenced_memory_ids.update(item.get("memory_id") for item in items if item.get("memory_id"))
    return {
        "project": project,
        "artifact_counts": counts,
        "orphan_counts": orphan_counts,
        "backed_memory_count": len(referenced_memory_ids),
        "unreferenced_memory_count": sum(1 for item in _load_live_memories() if (not project or item.get("project") == project) and item.get("source_id") not in referenced_memory_ids),
    }


def _memory_gc_candidates(request: MemoryGcCandidatesRequest) -> dict:
    candidates = []
    referenced_memory_ids = set()
    for _, (loader, _) in _artifact_store_pairs().items():
        for item in loader():
            if request.project and item.get("project") != request.project:
                continue
            if item.get("memory_id"):
                referenced_memory_ids.add(item["memory_id"])
    for item in _load_live_memories():
        if not _memory_matches_filters(item, project=request.project, include_archived=True):
            continue
        reasons = []
        age_days = _record_age_days(item)
        metadata = item.get("metadata") or {}
        if _is_archived_memory(item) and age_days >= request.stale_days:
            reasons.append("archived_stale")
        if item.get("source_id") not in referenced_memory_ids and not (metadata.get("pinned")):
            reasons.append("unreferenced")
        if not (metadata.get("source_refs") or []) and not (metadata.get("files") or []):
            reasons.append("no_source_refs")
        if (metadata.get("confidence") is not None) and metadata.get("confidence") < 0.4:
            reasons.append("low_confidence")
        if reasons:
            candidates.append({"reasons": reasons, "age_days": round(age_days, 1), "memory": _serialize_memory_record(item)})
    candidates.sort(key=lambda item: (len(item["reasons"]), item["age_days"]), reverse=True)
    return {"project": request.project, "items": candidates[: max(min(request.limit, 200), 1)]}


def _memory_compaction_report(request: MemoryCompactionReportRequest) -> dict:
    items = []
    for item in _load_live_memories():
        if not _memory_matches_filters(item, project=request.project, include_archived=False):
            continue
        content = item.get("content") or ""
        line_count = len(content.splitlines())
        char_count = len(content)
        looks_loggy = line_count >= request.min_lines or char_count >= request.min_chars
        if looks_loggy:
            items.append({"char_count": char_count, "line_count": line_count, "memory": _serialize_memory_record(item)})
    items.sort(key=lambda item: (item["char_count"], item["line_count"]), reverse=True)
    return {"project": request.project, "items": items[: max(min(request.limit, 200), 1)]}


def _link_cycle_detect(request: LinkCycleDetectRequest) -> dict:
    links = [item for item in _load_memory_links() if not request.project or item.get("project") == request.project]
    graph: dict[str, list[str]] = {}
    for item in links:
        graph.setdefault(item.get("source_id"), []).append(item.get("target_id"))
    cycles = []
    visited = set()

    def dfs(node: str, path: list[str], seen: set[str]) -> None:
        if len(cycles) >= max(min(request.limit, 200), 1):
            return
        visited.add(node)
        for neighbor in graph.get(node, []):
            if neighbor in seen:
                start = path.index(neighbor) if neighbor in path else 0
                cycles.append(path[start:] + [neighbor])
                continue
            dfs(neighbor, path + [neighbor], seen | {neighbor})

    for node in list(graph):
        if node not in visited:
            dfs(node, [node], {node})
    return {"project": request.project, "cycles": cycles[: max(min(request.limit, 200), 1)], "count": len(cycles)}


def _link_orphan_scan(project: str | None = None) -> dict:
    memory_ids = {item.get("source_id") for item in _load_live_memories() if not project or item.get("project") == project}
    orphans = []
    for item in _load_memory_links():
        if project and item.get("project") != project:
            continue
        if item.get("source_id") not in memory_ids or item.get("target_id") not in memory_ids:
            orphans.append(_serialize_memory_link(item))
    return {"project": project, "orphan_links": orphans, "count": len(orphans)}


def _project_map_compare(request: ProjectMapCompareRequest) -> dict:
    left = _get_project_map(request.left_project)
    right = _get_project_map(request.right_project)
    left_sections = left.get("sections") or {}
    right_sections = right.get("sections") or {}
    keys = sorted(set(left_sections) | set(right_sections))
    comparison = []
    for key in keys:
        left_value = left_sections.get(key, "")
        right_value = right_sections.get(key, "")
        comparison.append({"section": key, "same": left_value == right_value, "left": left_value, "right": right_value})
    return {"left_project": request.left_project, "right_project": request.right_project, "sections": comparison}


def _validation_trend_report(request: ValidationTrendReportRequest) -> dict:
    items = [item for item in _load_validation_snapshots() if item.get("project") == request.project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "")
    trend = [{"at": item.get("updated_at") or item.get("created_at"), "overall_status": item.get("overall_status"), "lint": item.get("lint"), "tests": item.get("tests"), "smoke": item.get("smoke")} for item in items[-max(min(request.limit, 200), 1):]]
    status_counts = Counter((item.get("overall_status") or "unknown").lower() for item in items)
    return {"project": request.project, "trend": trend, "status_counts": dict(status_counts)}


def _entity_search(request: EntitySearchRequest) -> dict:
    catalog = _entity_catalog_get(request.project)
    query = request.query.lower()
    matches = []
    for kind in ("files", "endpoints", "env_vars", "concepts"):
        for value, count in (catalog.get(kind) or {}).items():
            if query in value.lower():
                matches.append({"kind": kind, "value": value, "count": count})
    matches.sort(key=lambda item: item["count"], reverse=True)
    return {"project": request.project, "query": request.query, "matches": matches[: max(min(request.limit, 200), 1)]}


def _entity_link_memories(request: EntityLinkMemoriesRequest) -> dict:
    candidates = []
    needle = request.entity.lower()
    for item in _load_live_memories():
        if not _memory_matches_filters(item, project=request.project, include_archived=False):
            continue
        haystack = " ".join([item.get("content") or "", " ".join(item.get("tags") or []), " ".join((item.get("metadata") or {}).get("files") or []), " ".join((item.get("metadata") or {}).get("source_refs") or [])]).lower()
        if needle in haystack:
            candidates.append(item)
    candidates = candidates[: max(min(request.limit, 50), 2)]
    links = []
    for idx, left in enumerate(candidates):
        for right in candidates[idx + 1:]:
            link = _create_memory_link(MemoryLinkRequest(source_id=left["source_id"], target_id=right["source_id"], relation=request.relation, project=request.project, metadata={"entity": request.entity}))
            links.append(_serialize_memory_link(link))
    return {"project": request.project, "entity": request.entity, "matched_memory_ids": [item["source_id"] for item in candidates], "links": links, "count": len(links)}


def _alias_stats(project: str | None = None) -> dict:
    alias_counter = Counter()
    alias_projects = {}
    for item in _load_live_memories():
        if project and item.get("project") != project:
            continue
        for alias in (item.get("metadata") or {}).get("aliases") or []:
            alias_counter[alias] += 1
            alias_projects.setdefault(alias, set()).add(item.get("project"))
    duplicates = [{"alias": alias, "count": count, "projects": sorted(alias_projects.get(alias) or [])} for alias, count in alias_counter.items() if count > 1]
    return {"project": project, "alias_count": sum(alias_counter.values()), "unique_alias_count": len(alias_counter), "duplicates": sorted(duplicates, key=lambda item: item["count"], reverse=True)}


def _relation_prune_low_quality(request: RelationPruneLowQualityRequest) -> dict:
    links = _load_memory_links()
    kept = []
    removed = []
    for item in links:
        if request.project and item.get("project") != request.project:
            kept.append(item)
            continue
        metadata = item.get("metadata") or {}
        confidence = metadata.get("confidence")
        quality = metadata.get("quality_score")
        remove = False
        if confidence is not None and confidence <= request.max_confidence:
            remove = True
        if quality is not None and quality <= request.max_quality_score:
            remove = True
        if request.remove_unscored and confidence is None and quality is None:
            remove = True
        if remove:
            removed.append(_serialize_memory_link(item))
        else:
            kept.append(item)
    _save_memory_links(kept)
    return {"project": request.project, "removed": removed, "count": len(removed)}


def _project_similarity_report(request: ProjectSimilarityReportRequest) -> dict:
    projects = sorted({item.get("project") for item in _load_live_memories() if item.get("project") and item.get("project") != request.project})
    base_records = [item for item in _load_live_memories() if item.get("project") == request.project]
    base_tags = set(tag for item in base_records for tag in (item.get("tags") or []))
    base_files = set(file for item in base_records for file in ((item.get("metadata") or {}).get("files") or []))
    scores = []
    for project in projects:
        records = [item for item in _load_live_memories() if item.get("project") == project]
        tags = set(tag for item in records for tag in (item.get("tags") or []))
        files = set(file for item in records for file in ((item.get("metadata") or {}).get("files") or []))
        shared_tags = base_tags & tags
        shared_files = base_files & files
        score = len(shared_tags) * 1.0 + len(shared_files) * 1.5
        scores.append({"project": project, "score": round(score, 3), "shared_tags": sorted(list(shared_tags))[:10], "shared_files": sorted(list(shared_files))[:10]})
    scores.sort(key=lambda item: item["score"], reverse=True)
    return {"project": request.project, "similar_projects": scores[: max(min(request.limit, 200), 1)]}


def _memory_changelog(request: MemoryChangelogRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.get("metadata") or {}
    events = list(metadata.get("changelog") or [])
    inferred = [{"at": record.get("created_at"), "action": "created", "details": {}}]
    for key, action in (("archived_at", "archived_at"), ("restored_at", "restored_at"), ("redacted_at", "redacted_at"), ("confidence_updated_at", "confidence_updated_at")):
        if metadata.get(key):
            inferred.append({"at": metadata.get(key), "action": action, "details": {}})
    if metadata.get("quality_votes"):
        for vote in metadata.get("quality_votes")[-10:]:
            inferred.append({"at": vote.get("at"), "action": "vote_quality", "details": vote})
    all_events = [item for item in inferred + events if item.get("at")]
    all_events.sort(key=lambda item: item.get("at"))
    return {"memory": _serialize_memory_record(record), "events": all_events[-max(min(request.limit, 200), 1):]}


def _review_queue_apply(request: ReviewQueueApplyRequest) -> dict:
    queue = _memory_review_queue(
        MemoryReviewQueueRequest(
            project=request.project,
            limit=max(request.limit, len(request.queue_ids), 1),
            include_conflicts=True,
            include_closed=bool(request.queue_ids),
        )
    ).get("items", [])
    if request.queue_ids:
        requested_ids = {str(value).strip() for value in request.queue_ids if str(value).strip()}
        queue = [item for item in queue if item.get("queue_id") in requested_ids]
    applied: list[dict] = []
    effective_status = request.status if request.mark_needs_review else None
    missing_queue_ids = sorted(
        {str(value).strip() for value in request.queue_ids if str(value).strip()}
        - {str(item.get("queue_id")) for item in queue}
    )
    for item in queue:
        memory_ids = [str(value) for value in item.get("memory_ids") or []]
        records = [
            _find_live_memory(memory_id, request.project)
            for memory_id in memory_ids
        ]
        records = [record for record in records if record is not None]
        if not records:
            continue
        before_statuses = [_review_status(record) for record in records]
        redacted_ids: list[str] = []
        if request.auto_redact_secrets:
            for record in records:
                redacted = _memory_redact(MemoryRedactRequest(id=record["source_id"], project=record.get("project")))
                if redacted.get("replacements", 0):
                    redacted_ids.append(record["source_id"])
        updated_records: list[dict] = []
        for memory_id in memory_ids:
            record = _find_live_memory(memory_id, request.project)
            if record is None:
                continue
            metadata = record.setdefault("metadata", {})
            status_changed = bool(effective_status and _review_status(record) != effective_status)
            was_redacted = memory_id in redacted_ids
            if not status_changed and not was_redacted:
                updated_records.append(record)
                continue
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            if effective_status:
                metadata["review_status"] = effective_status
                metadata["review_updated_at"] = now
                metadata["review_reason"] = ",".join(item.get("reasons") or [])
                metadata["review_queue_id"] = item.get("queue_id")
            _append_memory_changelog(
                record,
                "review_queue_apply",
                {
                    "queue_id": item.get("queue_id"),
                    "kind": item.get("kind"),
                    "status": effective_status,
                    "reasons": item.get("reasons") or [],
                    "redacted": record["source_id"] in redacted_ids,
                },
            )
            record["updated_at"] = now
            _replace_live_memory(record)
            updated_records.append(record)
        if effective_status and all(status == effective_status for status in before_statuses) and not redacted_ids:
            action = "already_" + effective_status
        elif not effective_status and not redacted_ids:
            action = "already_noop"
        else:
            action = "updated"
        applied.append(
            {
                "queue_id": item.get("queue_id"),
                "kind": item.get("kind"),
                "action": action,
                "status": effective_status or _combined_review_status(updated_records),
                "memory_ids": [record["source_id"] for record in updated_records],
                "memories": [_serialize_memory_record(record) for record in updated_records],
            }
        )
    return {
        "project": request.project,
        "status": effective_status,
        "count": len(applied),
        "items": applied,
        "missing_queue_ids": missing_queue_ids,
    }


def _triage_queue_apply(request: TriageQueueApplyRequest) -> dict:
    created = _relation_apply_suggestions(
        RelationApplySuggestionsRequest(
            project=request.project,
            min_score=request.min_score,
            limit=request.limit,
            include_relates_to=request.include_relates_to,
        )
    )
    return {"project": request.project, "count": created.get("count", 0), "links": created.get("links", [])}


def _artifact_batch_delete(request: ArtifactBatchDeleteRequest) -> dict:
    results = []
    for artifact_id in request.artifact_ids:
        results.append(
            _artifact_delete(
                ArtifactDeleteRequest(
                    artifact_type=request.artifact_type,
                    artifact_id=artifact_id,
                    project=request.project,
                    delete_backing_memory=request.delete_backing_memory,
                )
            )
        )
    return {"artifact_type": request.artifact_type, "count": len(results), "items": results}


def _artifact_batch_relink(request: ArtifactBatchRelinkRequest) -> dict:
    results = []
    for item in request.items:
        results.append(
            _orphan_artifact_relink(
                OrphanArtifactRelinkRequest(
                    artifact_type=request.artifact_type,
                    artifact_id=item["artifact_id"],
                    target_memory_id=item["target_memory_id"],
                    project=request.project or item.get("project"),
                )
            )
        )
    return {"artifact_type": request.artifact_type, "count": len(results), "items": results}


def _artifact_batch_restore(request: ArtifactBatchRestoreRequest) -> dict:
    results = []
    for artifact_id in request.artifact_ids:
        results.append(
            _artifact_restore(
                ArtifactRestoreRequest(
                    artifact_type=request.artifact_type,
                    artifact_id=artifact_id,
                    project=request.project,
                )
            )
        )
    return {"artifact_type": request.artifact_type, "count": len(results), "items": results}


def _schema_validate_strict(request: StrictSchemaValidateRequest) -> dict:
    memory_items = []
    for item in _load_live_memories():
        if not _memory_matches_filters(item, project=request.project, include_archived=request.include_archived):
            continue
        serialized = _serialize_memory_record(item)
        missing = []
        for field in ("id", "project", "type", "content", "created_at", "updated_at", "metadata"):
            if serialized.get(field) in (None, "", []):
                if field != "metadata":
                    missing.append(field)
        if "raw_title" not in (serialized.get("metadata") or {}):
            missing.append("metadata.raw_title")
        if "files" not in (serialized.get("metadata") or {}):
            missing.append("metadata.files")
        if "source_refs" not in (serialized.get("metadata") or {}):
            missing.append("metadata.source_refs")
        if missing:
            memory_items.append({"memory": serialized, "missing_fields": missing})
    artifact_items = _artifact_integrity_audit(request.project).get("orphans", {})
    return {"project": request.project, "memory_issues": memory_items, "artifact_orphans": artifact_items, "ok": not memory_items and not artifact_items}


def _normalize_memory_metadata(project: str | None = None) -> dict:
    live_records = _load_live_memories()
    updated = 0
    for item in live_records:
        if project and item.get("project") != project:
            continue
        metadata = item.setdefault("metadata", {})
        changed = False
        if "raw_title" not in metadata:
            metadata["raw_title"] = _build_memory_title(item.get("content") or "")
            changed = True
        if "files" not in metadata:
            metadata["files"] = []
            changed = True
        if "source_refs" not in metadata:
            metadata["source_refs"] = []
            changed = True
        if "aliases" not in metadata:
            metadata["aliases"] = []
            changed = True
        if "changelog" not in metadata:
            metadata["changelog"] = []
            changed = True
        if changed:
            updated += 1
    _save_live_memories(live_records)
    return {"project": project, "updated": updated}


def _integrity_repair_strict(request: IntegrityRepairStrictRequest) -> dict:
    repairs = []
    if request.normalize_metadata:
        repairs.append({"normalize_metadata": _normalize_memory_metadata(request.project)})
    repairs.append(
        {
            "repair_live_indexes": _repair_live_indexes(
                RepairLiveIndexesRequest(
                    remove_orphan_links=request.remove_orphan_links,
                    remove_orphan_artifacts=request.remove_orphan_artifacts,
                )
            )
        }
    )
    return {"project": request.project, "repairs": repairs, "strict_validation": _schema_validate_strict(StrictSchemaValidateRequest(project=request.project))}


def _admin_snapshot_path(value: str | Path, *, require_leaf: bool = False) -> Path:
    root = settings.runtime_dir / "admin-exports"
    try:
        return resolve_under_root(root, value, require_leaf=require_leaf)
    except SecurityBoundaryError as exc:
        raise HTTPException(status_code=400, detail="admin snapshot path must remain under admin-exports") from exc


def _admin_export(request: AdminExportRequest) -> dict:
    export_dir = settings.runtime_dir / "admin-exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    export_name = request.export_name or f"bhm-admin-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    target = _admin_snapshot_path(export_name, require_leaf=True)
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "project": request.project,
        "include_archived": request.include_archived,
        "memories": [
            item for item in _load_live_memories()
            if _memory_matches_filters(item, project=request.project, include_archived=request.include_archived)
        ],
        "links": [item for item in _load_memory_links() if not request.project or item.get("project") == request.project],
        "artifacts": {},
    }
    if request.include_artifacts:
        for artifact_type, (loader, _) in _artifact_store_pairs().items():
            payload["artifacts"][artifact_type] = [item for item in loader() if not request.project or item.get("project") == request.project]
    # lgtm [py/path-injection]
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target), "memory_count": len(payload["memories"]), "link_count": len(payload["links"]), "artifact_counts": {key: len(value) for key, value in payload["artifacts"].items()}}


def _admin_import_preview(request: AdminImportPreviewRequest) -> dict:
    path = _admin_snapshot_path(request.path)
    # lgtm [py/path-injection]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="import path not found")
    # lgtm [py/path-injection]
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "path": str(path),
        "project": payload.get("project"),
        "memory_count": len(payload.get("memories") or []),
        "link_count": len(payload.get("links") or []),
        "artifact_counts": {key: len(value) for key, value in (payload.get("artifacts") or {}).items()},
    }


def _admin_import_apply(request: AdminImportApplyRequest) -> dict:
    path = _admin_snapshot_path(request.path)
    # lgtm [py/path-injection]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="import path not found")
    # lgtm [py/path-injection]
    payload = json.loads(path.read_text(encoding="utf-8"))
    imported = {"memories": 0, "links": 0, "artifacts": 0}
    live_records = _load_live_memories()
    by_id = {item.get("source_id"): item for item in live_records}
    for item in payload.get("memories") or []:
        if request.merge_mode == "replace" or item.get("source_id") not in by_id:
            by_id[item.get("source_id")] = item
            imported["memories"] += 1
    _save_live_memories(list(by_id.values()))
    links = _load_memory_links()
    seen = {(item.get("source_id"), item.get("target_id"), item.get("relation"), item.get("project")) for item in links}
    for item in payload.get("links") or []:
        key = (item.get("source_id"), item.get("target_id"), item.get("relation"), item.get("project"))
        if request.merge_mode == "replace" or key not in seen:
            links.append(item)
            seen.add(key)
            imported["links"] += 1
    _save_memory_links(links)
    for artifact_type, items in (payload.get("artifacts") or {}).items():
        pair = _artifact_store_pairs().get(artifact_type)
        if not pair:
            continue
        loader, saver = pair
        existing = loader()
        indexed = {item.get("id"): item for item in existing}
        for item in items:
            if request.merge_mode == "replace" or item.get("id") not in indexed:
                indexed[item.get("id")] = item
                imported["artifacts"] += 1
        saver(list(indexed.values()))
    return {"path": str(path), "merge_mode": request.merge_mode, "imported": imported}


def _policy_profile_set(request: PolicyProfileSetRequest) -> dict:
    profile = request.model_dump()
    profile["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_policy_profile(profile)
    return profile


def _policy_enforce_memory(request: PolicyEnforceMemoryRequest) -> dict:
    record = _find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    profile = _load_policy_profile()
    content = record.get("content") or ""
    issues = []
    if profile.get("require_project") and not record.get("project"):
        issues.append("missing_project")
    if profile.get("require_memory_type") and not record.get("memory_type"):
        issues.append("missing_memory_type")
    if len(content) > int(profile.get("max_content_chars") or 8000):
        issues.append("content_too_large")
    if content.count("\n") > int(profile.get("max_lines") or 120):
        issues.append("too_many_lines")
    secret_like = contains_secret_like(content)
    if profile.get("block_secret_like") and secret_like:
        issues.append("secret_like")
        if request.auto_redact:
            _memory_redact(MemoryRedactRequest(id=request.id, project=request.project))
            record = _find_live_memory(request.id, request.project) or record
    raw_log_like = content.count("\n") > int(profile.get("max_lines") or 120)
    if profile.get("block_raw_logs") and raw_log_like:
        issues.append("raw_log_like")
    return {"memory": _serialize_memory_record(record), "profile": profile, "issues": issues, "ok": not issues}


def _overlap_report(request: OverlapReportRequest) -> dict:
    duplicates = _detect_duplicates(MemoryDetectRequest(project=request.project, limit=request.limit, include_archived=False))
    same_upsert = []
    by_key = {}
    for item in _load_live_memories():
        if not _memory_matches_filters(item, project=request.project, include_archived=True):
            continue
        key = ((item.get("metadata") or {}).get("upsert_key") or "")
        if not key:
            continue
        by_key.setdefault(key, []).append(item.get("source_id"))
    for key, ids in by_key.items():
        if len(ids) > 1:
            same_upsert.append({"upsert_key": key, "ids": ids})
    return {"project": request.project, "duplicate_candidates": duplicates[: max(min(request.limit, 200), 1)], "same_upsert_key": same_upsert}


def _overlap_cleanup_apply(request: OverlapCleanupApplyRequest) -> dict:
    duplicates = _detect_duplicates(MemoryDetectRequest(project=request.project, limit=request.limit, include_archived=False))
    merged = []
    seen_sources = set()
    for item in duplicates:
        source_id = item["right_id"]
        target_id = item["left_id"]
        if source_id in seen_sources or target_id in seen_sources:
            continue
        result = _merge_memories(MemoryMergeRequest(project=request.project, source_id=source_id, target_id=target_id, archive_source=request.archive_sources))
        merged.append(result)
        seen_sources.add(source_id)
    return {"project": request.project, "count": len(merged), "items": merged}


def _resolve_slot(project: str, label: str) -> dict | None:
    for item in _load_slots():
        if item.get("project") == project and item.get("label") == label:
            return item
    return None


def _rerank_vector_results(query: str, results: list[dict]) -> list[dict]:
    reranked: list[tuple[float, dict]] = []
    for item in results:
        total = float(item.get("score") or 0.0)
        total += _lexical_signal(query, item)
        total += _memory_type_weight(item)
        total += _query_intent_weight(query, item)
        reranked.append((total, item))

    reranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in reranked]


def _float_score(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _apply_decay_to_vector_hit(hit: dict, now: datetime) -> dict:
    metadata = ensure_decay_metadata(
        dict(hit.get("metadata") or {}),
        fallback_at=(
            hit.get("last_accessed_at")
            or hit.get("updated_at")
            or hit.get("created_at")
            or (hit.get("metadata") or {}).get("updated_at")
            or (hit.get("metadata") or {}).get("created_at")
            or _utc_now_iso()
        ),
    )
    raw_score = _float_score(hit.get("score"))
    final_score = memory_decay_score(metadata, raw_qdrant_score=raw_score, now=now)
    metadata["raw_qdrant_score"] = raw_score
    metadata["decay_score"] = final_score
    metadata["decay_lambda_per_day"] = decay_lambda_for_payload(metadata)
    hit["metadata"] = metadata
    hit["score"] = final_score
    return hit


def _apply_decay_to_vector_hits(hits: list[dict], now: datetime | None = None) -> list[dict]:
    current = now or datetime.now(timezone.utc)
    return [_apply_decay_to_vector_hit(hit, current) for hit in hits]


def _access_updates_for_hits(hits: list[dict], accessed_at: str) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        metadata = hit.get("metadata") or {}
        collection_name = _search_hit_collection(hit)
        point_id = hit.get("id") or metadata.get("mem0_hit_id")
        if not collection_name or not point_id:
            continue
        key = (collection_name, str(point_id))
        if key in seen:
            continue
        seen.add(key)
        updates.append(
            {
                "collection_name": collection_name,
                "point_id": point_id,
                "payload": {
                    "access_count": normalize_access_count(metadata.get("access_count")) + 1,
                    "last_accessed_at": accessed_at,
                },
            }
        )
    return updates


def _update_vector_access_payloads(updates: list[dict[str, Any]]) -> None:
    client = get_qdrant_client()
    for update in updates:
        try:
            client.set_payload(
                collection_name=update["collection_name"],
                payload=update["payload"],
                points=[update["point_id"]],
            )
        except Exception as exc:
            print(f"[WARN] BHM decay access update failed: {exc}", flush=True)


async def _update_vector_access_payloads_async(updates: list[dict[str, Any]]) -> None:
    await asyncio.to_thread(_update_vector_access_payloads, updates)


def _schedule_vector_access_updates(hits: list[dict]) -> None:
    updates = _access_updates_for_hits(hits, _utc_now_iso())
    if not updates:
        return
    asyncio.create_task(_update_vector_access_payloads_async(updates))


def _search_hit_content(hit: dict) -> str:
    return str(hit.get("content") or hit.get("memory") or "")


def _search_hit_origin(hit: dict) -> str:
    metadata = hit.get("metadata") or {}
    origin = str(hit.get("context_origin") or metadata.get("context_origin") or "").upper()
    return origin if origin in {_VECTOR_CONTEXT_LOCAL, _VECTOR_CONTEXT_GLOBAL} else _VECTOR_CONTEXT_LOCAL


def _search_hit_collection(hit: dict) -> str:
    metadata = hit.get("metadata") or {}
    return str(hit.get("vector_collection") or metadata.get("vector_collection") or "")


def _hybrid_hit_key(hit: dict, fallback_index: int) -> str:
    metadata = hit.get("metadata") or {}
    candidates = [
        metadata.get("source_id"),
        hit.get("source_id"),
        metadata.get("upsert_key"),
        hit.get("id"),
        metadata.get("mem0_hit_id"),
    ]
    for candidate in candidates:
        key = str(candidate or "").strip()
        if key:
            return key
    content_key = _normalized_text(_search_hit_content(hit))
    if content_key:
        return f"content::{content_key}"
    return f"hit::{fallback_index}"


def _hybrid_hit_text(hit: dict) -> str:
    metadata = hit.get("metadata") or {}
    parts = [
        _search_hit_content(hit),
        metadata.get("raw_title"),
        " ".join(str(tag) for tag in metadata.get("tags") or []),
        " ".join(str(file) for file in metadata.get("files") or []),
        metadata.get("upsert_key"),
    ]
    return " ".join(str(part) for part in parts if str(part or "").strip())


def _rank_hybrid_vector_hits(query: str, hits: list[dict], *, k: int = 60) -> list[dict]:
    if not hits:
        return []

    indexed_hits: list[tuple[int, str, dict]] = []
    seen_keys: set[str] = set()
    for index, hit in enumerate(hits):
        key = _hybrid_hit_key(hit, index)
        if key in seen_keys:
            key = f"{key}::{index}"
        seen_keys.add(key)
        indexed_hits.append((index, key, hit))

    semantic_order = sorted(
        indexed_hits,
        key=lambda entry: (
            float(entry[2].get("score") or 0.0),
            1 if _search_hit_origin(entry[2]) == _VECTOR_CONTEXT_LOCAL else 0,
            str(
                entry[2].get("updated_at")
                or (entry[2].get("metadata") or {}).get("updated_at")
                or (entry[2].get("metadata") or {}).get("created_at")
                or ""
            ),
            -entry[0],
        ),
        reverse=True,
    )
    semantic_ranks = {key: rank for rank, (_, key, _) in enumerate(semantic_order, start=1)}

    lexical_scores = {key: lexical_score(query, _hybrid_hit_text(hit)) for _, key, hit in indexed_hits}
    lexical_order = sorted(
        indexed_hits,
        key=lambda entry: (
            -lexical_scores[entry[1]],
            semantic_ranks[entry[1]],
            entry[0],
        ),
    )
    lexical_ranks = {key: rank for rank, (_, key, _) in enumerate(lexical_order, start=1)}
    graph_candidates = [
        entry
        for entry in indexed_hits
        if float((entry[2].get("metadata") or {}).get("graph_score") or 0.0) > 0.0
    ]
    graph_order = sorted(
        graph_candidates,
        key=lambda entry: (
            -float((entry[2].get("metadata") or {}).get("graph_score") or 0.0),
            entry[0],
        ),
    )
    graph_ranks = {key: rank for rank, (_, key, _) in enumerate(graph_order, start=1)}
    fused_scores = weighted_rank_fusion(
        {
            "semantic": semantic_ranks,
            "lexical": lexical_ranks,
            "graph": graph_ranks,
        },
        k=k,
        # Graph expansions are useful corroboration, not a replacement for a
        # strong semantic/lexical match; keep their influence bounded.
        weights={"graph": _GRAPH_FUSION_WEIGHT},
    )

    ranked = sorted(
        indexed_hits,
        key=lambda entry: (
            -fused_scores[entry[1]],
            semantic_ranks[entry[1]],
            lexical_ranks[entry[1]],
            -float(entry[2].get("score") or 0.0),
            -lexical_scores[entry[1]],
            entry[0],
        ),
    )
    mmr_selections = mmr_select(
        [_hybrid_hit_text(hit) for _, _key, hit in ranked],
        [fused_scores[key] for _, key, _hit in ranked],
        lambda_param=_MMR_LAMBDA,
    )
    diversified: list[dict] = []
    for mmr_rank, selection in enumerate(mmr_selections, start=1):
        _index, key, hit = ranked[selection.index]
        metadata = dict(hit.get("metadata") or {})
        metadata["semantic_rank"] = semantic_ranks[key]
        metadata["lexical_rank"] = lexical_ranks[key]
        if key in graph_ranks:
            metadata["graph_rank"] = graph_ranks[key]
        metadata["fusion_channels"] = [
            "semantic",
            "lexical",
            *(["graph"] if key in graph_ranks else []),
        ]
        metadata["rrf_score"] = fused_scores[key]
        metadata["fusion_score"] = fused_scores[key]
        metadata["mmr_rank"] = mmr_rank
        metadata["mmr_score"] = selection.mmr_score
        metadata["diversity_penalty"] = selection.redundancy
        hit["metadata"] = metadata
        diversified.append(hit)

    return diversified


def _normalize_collection_hit(hit: dict, *, collection_name: str, context_origin: str) -> dict:
    item = dict(hit)
    metadata = dict(item.get("metadata") or {})
    metadata["context_origin"] = context_origin
    metadata["context_origins"] = [context_origin]
    metadata["vector_collection"] = collection_name
    metadata["vector_collections"] = [collection_name]
    content = _search_hit_content(item)
    item["memory"] = content
    item["content"] = content
    item["metadata"] = metadata
    item["context_origin"] = context_origin
    item["vector_collection"] = collection_name
    return item


def _qdrant_payload_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten projection metadata while retaining the canonical flat fields."""

    nested = payload.get("metadata")
    metadata = dict(nested) if isinstance(nested, dict) else {}
    for key, value in payload.items():
        if key not in {"content", "memory", "data", "metadata"}:
            metadata[key] = value
    return metadata


def _qdrant_point_to_vector_hit(
    point: Any,
    *,
    collection_name: str,
    context_origin: str,
    score_override: Any | None = None,
) -> dict | None:
    payload = dict(getattr(point, "payload", None) or {})
    content = str(payload.get("data") or payload.get("memory") or payload.get("content") or "").strip()
    if not content:
        return None
    metadata = _qdrant_payload_metadata(payload)
    metadata["mem0_hit_id"] = str(getattr(point, "id", "") or "")
    raw_score = payload.get("score") or payload.get("decay_score")
    if raw_score in (None, "") and score_override is not None:
        raw_score = score_override
    hit = {
        "id": str(getattr(point, "id", "") or ""),
        "content": content,
        "memory": content,
        "score": _float_score(raw_score),
        "metadata": metadata,
    }
    return _normalize_collection_hit(hit, collection_name=collection_name, context_origin=context_origin)


def _fetch_qdrant_hit_by_source_id_sync(target_id: str, project_name: str) -> dict | None:
    client = get_qdrant_client()
    source_id = str(target_id or "").strip()
    if not source_id:
        return None
    scroll_filter = qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="source_id",
                match=qdrant_models.MatchValue(value=source_id),
            )
        ]
    )
    collections = [
        (local_collection_name(project_name), _VECTOR_CONTEXT_LOCAL),
        (global_collection_name(), _VECTOR_CONTEXT_GLOBAL),
    ]
    for collection_name, context_origin in collections:
        try:
            points, _offset = client.scroll(
                collection_name=collection_name,
                scroll_filter=scroll_filter,
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            continue
        if not points:
            continue
        return _qdrant_point_to_vector_hit(points[0], collection_name=collection_name, context_origin=context_origin)

    for collection_name, context_origin in collections:
        try:
            points = client.retrieve(
                collection_name=collection_name,
                ids=[source_id],
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            continue
        if not points:
            continue
        return _qdrant_point_to_vector_hit(points[0], collection_name=collection_name, context_origin=context_origin)
    return None


async def _fetch_qdrant_hit_by_source_id(target_id: str, project_name: str) -> dict | None:
    return await asyncio.to_thread(_fetch_qdrant_hit_by_source_id_sync, target_id, project_name)


def _copy_graph_expansion_hit(hit: dict, *, source_id: str, link_type: str, graph_rank: int) -> dict:
    item = dict(hit)
    metadata = dict(item.get("metadata") or {})
    metadata["graph_metadata"] = {
        "is_graph_expansion": True,
        "extended_from": source_id,
        "link_type": link_type,
    }
    metadata["graph_rank"] = graph_rank
    metadata["graph_score"] = round(1.0 / max(graph_rank, 1), 6)
    item["metadata"] = metadata
    return item


async def _augment_hits_with_graph_expansion(hits: list[dict], project_name: str, now: datetime) -> list[dict]:
    if not hits:
        return hits

    existing_by_id: dict[str, dict] = {}
    for hit in hits:
        for node_id in _semantic_graph_node_aliases(hit):
            existing_by_id.setdefault(node_id, hit)

    seed_hits = hits[:2]
    seed_ids = {alias for hit in seed_hits for alias in _semantic_graph_node_aliases(hit)}
    expansions_by_source: dict[str, list[dict]] = {}
    promoted_ids: set[str] = set()
    graph_rank = 0

    for seed in seed_hits:
        source_aliases = _semantic_graph_node_aliases(seed)
        source_id = source_aliases[0] if source_aliases else ""
        if not source_aliases or not source_id:
            continue
        links: list[dict[str, str]] = []
        seen_link_keys: set[tuple[str, str]] = set()
        for source_alias in source_aliases:
            for link in await _BHM_GRAPH_MANAGER.get_linked_nodes(source_alias, _GRAPH_EXPANSION_EDGE_TYPES):
                link_key = (str(link.get("target_id") or ""), str(link.get("edge_type") or ""))
                if link_key in seen_link_keys:
                    continue
                seen_link_keys.add(link_key)
                links.append(link)
        for link in links:
            target_id = str(link.get("target_id") or "").strip()
            link_type = str(link.get("edge_type") or "").strip().upper()
            if not target_id or target_id == source_id or target_id in seed_ids or target_id in promoted_ids:
                continue
            target_hit = existing_by_id.get(target_id)
            if target_hit is None:
                target_hit = await _fetch_qdrant_hit_by_source_id(target_id, project_name)
                if target_hit is not None:
                    target_hit = _apply_decay_to_vector_hit(target_hit, now)
            if target_hit is None:
                continue
            graph_rank += 1
            expansions_by_source.setdefault(source_id, []).append(
                _copy_graph_expansion_hit(
                    target_hit,
                    source_id=source_id,
                    link_type=link_type,
                    graph_rank=graph_rank,
                )
            )
            promoted_ids.add(target_id)

    if not promoted_ids:
        return hits

    expanded: list[dict] = []
    for hit in hits:
        aliases = _semantic_graph_node_aliases(hit)
        node_id = aliases[0] if aliases else ""
        if any(alias in promoted_ids for alias in aliases):
            continue
        expanded.append(hit)
        if node_id:
            expanded.extend(expansions_by_source.get(node_id, []))
    return expanded


def _galaxy_memory_collection_names(client: Any, project: str | None) -> list[str]:
    global_name = global_collection_name()
    if project:
        return list(dict.fromkeys([local_collection_name(project), global_name]))

    try:
        collections = client.get_collections().collections
    except Exception:
        return list(dict.fromkeys([local_collection_name(settings.qdrant_collection), global_name]))

    names: list[str] = []
    for collection in collections:
        name = str(getattr(collection, "name", "") or "")
        if name == global_name or name.startswith("bhm_local_memory_"):
            names.append(name)
    if global_name not in names:
        names.append(global_name)
    return list(dict.fromkeys(names))


def _galaxy_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[\s,|]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _galaxy_payload_is_active(payload: dict[str, Any]) -> bool:
    lifecycle = str(payload.get("lifecycle") or payload.get("status") or "").strip().lower()
    if lifecycle in {"archived", "deprecated", "deleted", "evicted"}:
        return False
    return not any(payload.get(key) for key in ("archived_at", "archive_reason", "deleted_at", "evicted_at"))


def _galaxy_payload_text(payload: dict[str, Any]) -> str:
    text = (
        payload.get("core_insight")
        or payload.get("data")
        or payload.get("memory")
        or payload.get("content")
        or payload.get("raw_title")
        or payload.get("summary")
        or ""
    )
    return str(text or "").strip()


def _galaxy_label_from_text(text: str, fallback: str) -> str:
    first_line = str(text or "").strip().splitlines()[0].strip() if text else ""
    return (first_line or fallback)[:96]


def _galaxy_node_color(payload: dict[str, Any], collection_name: str) -> str:
    if collection_name == global_collection_name():
        return "#ffd166"
    domain = str(payload.get("domain") or "").lower()
    if domain == "frontend":
        return "#3b82f6"
    if domain == "backend":
        return "#ef4444"
    if domain == "security":
        return "#f97316"
    if domain == "product":
        return "#22c55e"
    return "#87f5c9"


def _galaxy_node_value(payload: dict[str, Any]) -> float:
    importance = normalize_importance_score(payload.get("importance_score"))
    access_count = min(normalize_access_count(payload.get("access_count")), 20)
    return round(3.2 + min(importance / 2.0, 5.0) + min(access_count / 12.0, 1.6), 2)


def _galaxy_point_node(point: Any, *, collection_name: str) -> tuple[dict[str, Any], list[str]] | None:
    payload = dict(getattr(point, "payload", None) or {})
    if not payload or not _galaxy_payload_is_active(payload):
        return None

    point_id = str(getattr(point, "id", "") or "").strip()
    source_id = str(payload.get("source_id") or payload.get("id") or point_id).strip()
    if not source_id and not point_id:
        return None

    node_id = source_id or point_id
    core_insight = _galaxy_payload_text(payload)
    tags = _galaxy_string_list(payload.get("tags") or payload.get("concepts"))
    project = _canonical_project(
        str(payload.get("project") or payload.get("project_name") or settings.qdrant_collection or "").strip()
    )
    memory_type = str(payload.get("memory_type") or payload.get("type") or "memory").strip() or "memory"
    node = {
        "id": node_id,
        "label": _galaxy_label_from_text(core_insight, node_id),
        "type": "memory",
        "val": _galaxy_node_value(payload),
        "color": _galaxy_node_color(payload, collection_name),
        "core_insight": core_insight[:1200],
        "tags": tags,
        "metadata": {
            key: value
            for key, value in payload.items()
            if key not in {"data", "memory", "content", "core_insight"}
        },
        "meta": {
            "project": project,
            "project_key": project,
            "memory_type": memory_type,
            "source_id": source_id,
            "product_root": "BlackHoleMemory",
            "galaxy_domain": "memory",
            "source_layer": "bhm",
            "qdrant_point_id": point_id,
            "vector_collection": collection_name,
            "tags": tags,
            "content_preview": core_insight[:320],
        },
    }
    aliases = [node_id, source_id, point_id, payload.get("mem0_hit_id")]
    aliases.extend(_galaxy_string_list(payload.get("mem0_ids")))
    aliases.extend(_galaxy_string_list(payload.get("global_mem0_ids")))
    return node, list(dict.fromkeys(str(alias).strip() for alias in aliases if str(alias or "").strip()))


def _merge_galaxy_node(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    existing_tags = _galaxy_string_list(existing.get("tags"))
    incoming_tags = _galaxy_string_list(incoming.get("tags"))
    existing["tags"] = list(dict.fromkeys(existing_tags + incoming_tags))
    existing_meta = existing.setdefault("meta", {})
    incoming_meta = incoming.get("meta") or {}
    collections = _galaxy_string_list(existing_meta.get("vector_collections") or existing_meta.get("vector_collection"))
    collections.extend(_galaxy_string_list(incoming_meta.get("vector_collection")))
    existing_meta["vector_collections"] = list(dict.fromkeys(collections))
    if not existing.get("core_insight") and incoming.get("core_insight"):
        existing["core_insight"] = incoming["core_insight"]
    if not existing_meta.get("qdrant_point_id") and incoming_meta.get("qdrant_point_id"):
        existing_meta["qdrant_point_id"] = incoming_meta["qdrant_point_id"]


def _load_galaxy_active_nodes_sync(project: str | None, limit: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    client = get_qdrant_client()
    nodes_by_id: dict[str, dict[str, Any]] = {}
    alias_to_node_id: dict[str, str] = {}
    max_nodes = max(0, min(int(limit or 0), 5000))
    if max_nodes == 0:
        return [], {}

    for collection_name in _galaxy_memory_collection_names(client, project):
        offset = None
        while len(nodes_by_id) < max_nodes:
            try:
                points, offset = client.scroll(
                    collection_name=collection_name,
                    limit=min(256, max_nodes - len(nodes_by_id)),
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception:
                break
            if not points:
                break

            for point in points:
                item = _galaxy_point_node(point, collection_name=collection_name)
                if item is None:
                    continue
                node, aliases = item
                node_id = node["id"]
                if node_id in nodes_by_id:
                    _merge_galaxy_node(nodes_by_id[node_id], node)
                else:
                    nodes_by_id[node_id] = node
                for alias in aliases:
                    alias_to_node_id.setdefault(alias, node_id)

            if offset is None:
                break

    nodes = sorted(nodes_by_id.values(), key=lambda node: (str((node.get("meta") or {}).get("project") or ""), node["id"]))
    return nodes, alias_to_node_id


async def _load_galaxy_active_nodes(project: str | None, limit: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    return await asyncio.to_thread(_load_galaxy_active_nodes_sync, project, limit)


def _load_galaxy_code_project_nodes_sync(project: str | None, limit: int) -> list[dict[str, Any]]:
    """Return one bounded CBM project node per current SQLite graph snapshot.

    The Galaxy receives metadata-only code summaries.  Individual code symbols
    remain on the canonical CBM graph/search surfaces; this read model only
    provides the global code-domain project constellations.
    """

    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    accepted = _project_aliases(project) if project else set()
    nodes: list[dict[str, Any]] = []
    with sqlite3.connect(database_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT current.project, current.root_id, current.graph_snapshot_id,
                   snapshots.root_path, snapshots.repository_snapshot_id,
                   snapshots.graph_digest, snapshots.status, snapshots.summary_json
              FROM repository_code_graph_current AS current
              JOIN repository_code_graph_snapshots AS snapshots
                ON snapshots.graph_snapshot_id = current.graph_snapshot_id
             WHERE snapshots.status = 'completed'
             ORDER BY current.project
             LIMIT 256
            """
        ).fetchall()
    for row in rows:
        project_key = _canonical_project(str(row["project"] or "").strip())
        if not project_key or (accepted and project_key not in accepted):
            continue
        try:
            summary = json.loads(str(row["summary_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            summary = {}
        if not isinstance(summary, dict):
            summary = {}
        display = "BlackHoleMemory" if project_key == "blackholememory" else project_key
        nodes.append(
            {
                "id": f"project::{project_key}",
                "label": display,
                "type": "project",
                "val": 14.0,
                "color": "#4cc9f0",
                "core_insight": "CBM code graph project slice",
                "tags": ["CBM", "code-graph"],
                "metadata": {
                    "domain": "code",
                    "galaxy_domain": "code",
                    "source_layer": "cbm",
                    "product_root": "BlackHoleMemory",
                },
                "meta": {
                    "project": project_key,
                    "project_key": project_key,
                    "product_root": "BlackHoleMemory",
                    "galaxy_domain": "code",
                    "source_layer": "cbm",
                    "domains": ["code"],
                    "root_id": str(row["root_id"] or ""),
                    "graph_snapshot_id": str(row["graph_snapshot_id"] or ""),
                    "repository_snapshot_id": str(row["repository_snapshot_id"] or ""),
                    "graph_digest": str(row["graph_digest"] or ""),
                    "root_path": str(row["root_path"] or ""),
                    "node_count": int(summary.get("node_count") or 0),
                    "edge_count": int(summary.get("edge_count") or 0),
                    "parser_error_count": int(summary.get("parser_error_count") or 0),
                    "content_preview": "CBM metadata-only code graph slice",
                },
            }
        )
    return nodes[: max(0, min(int(limit or 0), 256))]


async def _load_galaxy_code_project_nodes(project: str | None, limit: int) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_load_galaxy_code_project_nodes_sync, project, limit)


async def _build_galaxy_data(project: str | None, limit: int, domain: str = "all") -> dict[str, Any]:
    selected_domain = str(domain or "all").strip().casefold()
    if selected_domain not in {"all", "memory", "code"}:
        selected_domain = "all"

    nodes, alias_to_node_id = await _load_galaxy_active_nodes(project, limit)
    for node in nodes:
        meta = node.setdefault("meta", {})
        metadata = node.setdefault("metadata", {})
        project_key = _canonical_project(str(meta.get("project") or metadata.get("project") or ""))
        if project_key:
            meta["project"] = project_key
            meta["project_key"] = project_key
        meta.setdefault("product_root", "BlackHoleMemory")
        meta.setdefault("galaxy_domain", "memory")
        meta.setdefault("source_layer", "bhm")
        metadata.setdefault("domain", "memory")

    # A project-scoped request may still read the global BHM collection.  Do
    # the final canonical project filter on the normalized read model so a
    # project slice never leaks memories from another repository.
    requested_project = _canonical_project(project) if project else ""
    if requested_project:
        nodes = [
            node
            for node in nodes
            if str((node.get("meta") or {}).get("project_key") or "") == requested_project
        ]

    code_nodes = await _load_galaxy_code_project_nodes(project, max(1, limit))
    graph = await _BHM_GRAPH_MANAGER.get_graph()
    links: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for source_alias, outgoing in graph.items():
        source = alias_to_node_id.get(str(source_alias or "").strip())
        if not source:
            continue
        for link in outgoing or []:
            target_alias = str((link or {}).get("target_id") or "").strip()
            target = alias_to_node_id.get(target_alias)
            edge_type = str((link or {}).get("edge_type") or "").strip().upper()
            if not target or source == target or edge_type not in _SEMANTIC_LINK_EDGE_TYPES:
                continue
            key = (source, target, edge_type)
            if key in seen:
                continue
            seen.add(key)
            links.append({"source": source, "target": target, "type": edge_type})

    # The global view has one product root, two domain hubs and one shared
    # project hub per canonical repository.  Memory and code never become two
    # product roots merely because they came from different authorities.
    if project is None:
        root_id = "galaxy-root::bhm"
        memory_domain_id = "galaxy-domain::memory"
        code_domain_id = "galaxy-domain::code"
        root = {
            "id": root_id,
            "label": "BlackHoleMemory",
            "type": "root",
            "val": 22.0,
            "color": "#f7f7f2",
            "metadata": {"domain": "cross_domain", "galaxy_domain": "cross_domain"},
            "meta": {"product_root": "BlackHoleMemory", "galaxy_domain": "cross_domain", "source_layer": "bhm+cbm"},
        }
        memory_domain = {
            "id": memory_domain_id,
            "label": "BHM memory",
            "type": "domain",
            "val": 15.0,
            "color": "#65a983",
            "metadata": {"domain": "memory", "galaxy_domain": "memory"},
            "meta": {"product_root": "BlackHoleMemory", "galaxy_domain": "memory", "source_layer": "bhm", "domains": ["memory"]},
        }
        code_domain = {
            "id": code_domain_id,
            "label": "CBM code",
            "type": "domain",
            "val": 15.0,
            "color": "#4cc9f0",
            "metadata": {"domain": "code", "galaxy_domain": "code"},
            "meta": {"product_root": "BlackHoleMemory", "galaxy_domain": "code", "source_layer": "cbm", "domains": ["code"]},
        }
        project_hubs: dict[str, dict[str, Any]] = {}
        for node in nodes:
            project_key = str((node.get("meta") or {}).get("project_key") or "").strip()
            if not project_key:
                continue
            hub = project_hubs.get(project_key)
            if hub is None:
                display = "BlackHoleMemory" if project_key == "blackholememory" else project_key
                hub = {
                    "id": f"project::{project_key}",
                    "label": display,
                    "type": "project",
                    "val": 13.0,
                    "color": "#cbd5e1",
                    "metadata": {"domain": "memory", "galaxy_domain": "memory"},
                    "meta": {"project": project_key, "project_key": project_key, "product_root": "BlackHoleMemory", "domains": ["memory"], "source_layer": "bhm"},
                }
                project_hubs[project_key] = hub
        for code_node in code_nodes:
            project_key = str((code_node.get("meta") or {}).get("project_key") or "").strip()
            if not project_key:
                continue
            hub = project_hubs.get(project_key)
            if hub is None:
                hub = dict(code_node)
                hub["meta"] = dict(code_node.get("meta") or {})
                hub["metadata"] = dict(code_node.get("metadata") or {})
                project_hubs[project_key] = hub
            else:
                hub_meta = hub.setdefault("meta", {})
                hub_meta["domains"] = list(dict.fromkeys([*(hub_meta.get("domains") or []), "code"]))
                hub_meta["source_layer"] = "bhm+cbm"
                hub_meta["galaxy_domain"] = "cross_domain"
                hub_meta.update({key: value for key, value in (code_node.get("meta") or {}).items() if key not in {"project", "project_key", "domains", "source_layer", "galaxy_domain"}})
                hub.setdefault("metadata", {})["domain"] = "cross_domain"
                hub["metadata"]["galaxy_domain"] = "cross_domain"
        nodes = [root, memory_domain, code_domain, *project_hubs.values(), *nodes]
        links.extend(
            [
                {"source": root_id, "target": memory_domain_id, "type": "root_branch"},
                {"source": root_id, "target": code_domain_id, "type": "root_branch"},
            ]
        )
        for project_key, hub in project_hubs.items():
            hub_id = str(hub["id"])
            hub_domains = set((hub.get("meta") or {}).get("domains") or [])
            if "memory" in hub_domains:
                links.append({"source": memory_domain_id, "target": hub_id, "type": "domain_project"})
            if "code" in hub_domains:
                links.append({"source": code_domain_id, "target": hub_id, "type": "domain_project"})
            for node in nodes:
                if node is hub:
                    continue
                node_meta = node.get("meta") or {}
                if node_meta.get("project_key") == project_key and node_meta.get("galaxy_domain") == "memory":
                    links.append({"source": hub_id, "target": str(node["id"]), "type": "belongs_to_project"})
    elif code_nodes:
        # Project-scoped views still expose the matching metadata-only CBM
        # slice.  The global root/hubs are omitted here to keep the narrowed
        # view compact; domain filtering below remains authoritative.
        nodes.extend(code_nodes)

    visible_domains = {"memory"} if selected_domain == "memory" else {"code"} if selected_domain == "code" else {"memory", "code", "cross_domain"}
    if selected_domain != "all":
        visible_ids = {
            str(node["id"])
            for node in nodes
            if str(node.get("id")) == "galaxy-root::bhm"
            or str((node.get("meta") or {}).get("galaxy_domain") or "") in visible_domains
            or selected_domain in set((node.get("meta") or {}).get("domains") or [])
        }
        nodes = [node for node in nodes if str(node["id"]) in visible_ids]
        links = [link for link in links if link["source"] in visible_ids and link["target"] in visible_ids]

    payload = GalaxyDataResponse(nodes=nodes, links=links)
    return payload.model_dump(mode="json")


def _search_memory_collection(
    *,
    query: str,
    project_name: str,
    context_origin: str,
    limit: int,
    candidate_filters: dict[str, Any] | None = None,
    query_embedding: Any | None = None,
) -> list[dict]:
    if not query.strip():
        return []
    if context_origin == _VECTOR_CONTEXT_GLOBAL:
        collection_name = global_collection_name()
        memory = get_global_core_memory()
    else:
        collection_name = local_collection_name(project_name)
        memory = get_project_mem0_memory(project_name)

    filters = candidate_filters or {"user_id": settings.mem0_user_id}
    if query_embedding is None:
        result = memory.search(query, top_k=max(limit, 1), filters=filters)
    else:
        result = search_with_precomputed_embedding(
            memory,
            query,
            query_embedding,
            top_k=max(limit, 1),
            filters=filters,
        )
    if isinstance(result, dict):
        raw_hits = result.get("results") or []
    elif isinstance(result, list):
        raw_hits = result
    else:
        raw_hits = []
    normalized_hits = [
        _normalize_collection_hit(hit, collection_name=collection_name, context_origin=context_origin)
        for hit in raw_hits
        if isinstance(hit, dict)
    ]
    return normalized_hits[: max(limit, 1)]


def _cached_query_embedding(memory: Any, query: str) -> Any:
    model_key = f"{settings.mem0_embedding_model}:{settings.mem0_embedding_dims}"
    vector, _cache_hit = embed_query_with_cache(
        memory.embedding_model,
        query,
        model_key=model_key,
        cache=_QUERY_EMBEDDING_CACHE,
    )
    return vector


def _merge_unique_strings(*values: Any) -> list[str]:
    merged: list[str] = []
    for value in values:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, (list, tuple, set)):
            candidates = list(value)
        else:
            candidates = []
        for candidate in candidates:
            item = str(candidate).strip()
            if item and item not in merged:
                merged.append(item)
    return merged


def merge_and_sort_hits(local_hits: list[dict], global_hits: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}

    def hit_key(hit: dict, fallback_index: int) -> str:
        content_key = _normalized_text(_search_hit_content(hit))
        if content_key:
            return f"content::{content_key}"
        metadata = hit.get("metadata") or {}
        stable_id = hit.get("id") or hit.get("source_id") or metadata.get("source_id") or metadata.get("upsert_key")
        return f"id::{stable_id or fallback_index}"

    def hit_rank(hit: dict) -> tuple[float, int, str]:
        origin_rank = 1 if _search_hit_origin(hit) == _VECTOR_CONTEXT_LOCAL else 0
        metadata = hit.get("metadata") or {}
        updated = str(hit.get("updated_at") or metadata.get("updated_at") or metadata.get("created_at") or "")
        return (float(hit.get("score") or 0.0), origin_rank, updated)

    for index, hit in enumerate([*local_hits, *global_hits]):
        key = hit_key(hit, index)
        origin = _search_hit_origin(hit)
        collection_name = _search_hit_collection(hit)
        current = merged.get(key)
        if current is None:
            metadata = dict(hit.get("metadata") or {})
            metadata["context_origins"] = _merge_unique_strings(metadata.get("context_origins"), origin)
            metadata["vector_collections"] = _merge_unique_strings(metadata.get("vector_collections"), collection_name)
            hit["metadata"] = metadata
            merged[key] = hit
            continue

        current_metadata = dict(current.get("metadata") or {})
        origins = _merge_unique_strings(current_metadata.get("context_origins"), origin)
        collections = _merge_unique_strings(current_metadata.get("vector_collections"), collection_name)
        if hit_rank(hit) > hit_rank(current):
            next_metadata = dict(hit.get("metadata") or {})
            next_metadata["context_origins"] = origins
            next_metadata["vector_collections"] = collections
            hit["metadata"] = next_metadata
            merged[key] = hit
        else:
            current_metadata["context_origins"] = origins
            current_metadata["vector_collections"] = collections
            current["metadata"] = current_metadata

    ordered = list(merged.values())
    ordered.sort(key=hit_rank, reverse=True)
    return ordered


def _vector_hit_matches_filters(
    hit: dict,
    *,
    project: str | None = None,
    memory_type: str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
) -> bool:
    metadata = hit.get("metadata") or {}
    accepted_projects = _project_aliases(project)
    if accepted_projects and metadata.get("project") not in accepted_projects:
        return False
    if memory_type and metadata.get("memory_type") != memory_type:
        return False
    if concepts and not set(concepts).issubset(set(metadata.get("tags") or [])):
        return False
    if files and not set(files).issubset(set(metadata.get("files") or [])):
        return False
    return _metadata_matches_taxonomy_filters(
        metadata,
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
        include_archived=include_archived,
        include_logs=include_logs,
    )


def _serialize_vector_hit(hit: dict) -> dict:
    metadata = dict(hit.get("metadata") or {})
    content = _search_hit_content(hit)
    context_origin = _search_hit_origin(hit)
    mem0_hit_id = hit.get("id")
    live_source_id = metadata.get("source_id") or hit.get("source_id") or mem0_hit_id
    if mem0_hit_id and live_source_id != mem0_hit_id:
        metadata.setdefault("mem0_hit_id", mem0_hit_id)
    return {
        "id": live_source_id,
        "title": metadata.get("raw_title") or _build_memory_title(content),
        "project": metadata.get("project"),
        "type": metadata.get("memory_type"),
        "content": content,
        "memory": content,
        "concepts": metadata.get("tags") or [],
        "files": metadata.get("files") or [],
        "source_system": metadata.get("source_system"),
        "agent_id": metadata.get("agent_id"),
        "created_at": metadata.get("created_at") or hit.get("created_at"),
        "updated_at": metadata.get("updated_at") or hit.get("updated_at"),
        "archived_at": metadata.get("archived_at"),
        "archive_reason": metadata.get("archive_reason"),
        "upsert_key": metadata.get("upsert_key"),
        "session_refs": metadata.get("session_refs") or [],
        "metadata": metadata,
        "score": float(hit.get("score") or 0.0),
        "context_origin": context_origin,
    }


def _context_item_from_vector_hit(hit: dict) -> dict[str, Any]:
    """Project a retrieval hit into the deliberately small context contract."""

    metadata = dict(hit.get("metadata") or {})
    content = _search_hit_content(hit).strip()
    live_source_id = metadata.get("source_id") or hit.get("source_id") or hit.get("id") or ""
    source_refs = metadata.get("source_refs") or hit.get("source_refs") or []
    files = metadata.get("files") or hit.get("files") or []
    source_system = metadata.get("source_system") or hit.get("source_system")
    source_kind = (
        metadata.get("source_kind")
        or metadata.get("provenance")
        or hit.get("source_kind")
        or hit.get("provenance")
    )
    agent_id = metadata.get("agent_id") or hit.get("agent_id")
    session_refs = metadata.get("session_refs") or hit.get("session_refs") or []
    context_origin = _search_hit_origin(hit)
    return {
        "id": live_source_id,
        "title": metadata.get("raw_title") or _build_memory_title(content),
        "project": metadata.get("project") or hit.get("project"),
        "content": content,
        "score": float(hit.get("score") or 0.0),
        "context_origin": context_origin,
        "metadata": {
            "source_refs": source_refs,
            "files": files,
            "source_id": live_source_id,
            "source_system": source_system,
            "source_kind": source_kind,
            "agent_id": agent_id,
            "session_refs": session_refs,
            "context_origin": context_origin,
        },
    }


def _strict_retrieval_hits(
    hits: list[dict],
    *,
    project_name: str,
    memory_type: str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    limit: int = 10,
) -> list[dict]:
    """Apply the authoritative post-filter before exposing retrieval diagnostics."""

    return [
        hit
        for hit in hits
        if _vector_hit_matches_filters(
            hit,
            project=project_name,
            memory_type=memory_type,
            concepts=concepts,
            files=files,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            include_archived=include_archived,
            include_logs=include_logs,
        )
    ][: max(int(limit), 1)]


async def federated_search(
    query: str,
    project_name: str,
    limit: int = 5,
    offset: int = 0,
    *,
    memory_type: str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    include_graph_expansion: bool = True,
    include_global: bool = True,
) -> tuple[list[dict], int]:
    project_name = _canonical_project(project_name)
    page_limit = max(min(limit, 200), 1)
    page_offset = max(offset, 0)
    candidate_count = max(page_limit + page_offset, 20)
    candidate_filters = build_candidate_filters(
        user_id=settings.mem0_user_id,
        project_values=_project_aliases(project_name),
        memory_type=memory_type,
        concepts=concepts or [],
        files=files or [],
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
        include_archived=include_archived,
        include_logs=include_logs,
    )
    query_embedding = None
    if query.strip():
        embedding_memory = await asyncio.to_thread(get_project_mem0_memory, project_name)
        query_embedding = await asyncio.to_thread(_cached_query_embedding, embedding_memory, query)

    local_task = asyncio.create_task(
        asyncio.to_thread(
            _search_memory_collection,
            query=query,
            project_name=project_name,
            context_origin=_VECTOR_CONTEXT_LOCAL,
            limit=candidate_count,
            candidate_filters=candidate_filters,
            query_embedding=query_embedding,
        )
    )
    tasks: list[asyncio.Task] = [local_task]
    if include_global:
        tasks.append(
            asyncio.create_task(
                asyncio.to_thread(
                    _search_memory_collection,
                    query=query,
                    project_name=project_name,
                    context_origin=_VECTOR_CONTEXT_GLOBAL,
                    limit=candidate_count,
                    candidate_filters=candidate_filters,
                    query_embedding=query_embedding,
                )
            )
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)
    local_hits: list[dict] = []
    global_hits: list[dict] = []
    errors: list[Exception] = []
    contours = (_VECTOR_CONTEXT_LOCAL, _VECTOR_CONTEXT_GLOBAL) if include_global else (_VECTOR_CONTEXT_LOCAL,)
    for context_origin, result in zip(contours, results, strict=True):
        if isinstance(result, Exception):
            errors.append(result)
            print(f"[WARN] BHM federated search {context_origin} contour failed: {result}", flush=True)
            continue
        if context_origin == _VECTOR_CONTEXT_LOCAL:
            local_hits = result
        else:
            global_hits = result
    if errors and not local_hits and not global_hits:
        raise errors[0]

    now = datetime.now(timezone.utc)
    local_hits = _apply_decay_to_vector_hits(local_hits, now)
    global_hits = _apply_decay_to_vector_hits(global_hits, now)
    combined_results = merge_and_sort_hits(local_hits, global_hits)
    filtered = [
        hit
        for hit in combined_results
        if _vector_hit_matches_filters(
            hit,
            project=project_name,
            memory_type=memory_type,
            concepts=concepts,
            files=files,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            include_archived=include_archived,
            include_logs=include_logs,
        )
    ]
    if include_graph_expansion:
        filtered = await _augment_hits_with_graph_expansion(filtered, project_name, now)
    ranked_hits = _rank_hybrid_vector_hits(query, filtered)
    total = len(ranked_hits)
    return ranked_hits[page_offset : page_offset + page_limit], total


def _synthesis_trim(value: Any, limit: int = _FACT_SYNTHESIS_MAX_ITEM_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _bounded_synthesis_zone(items: list[str]) -> list[str]:
    return [_synthesis_trim(item) for item in items[:_FACT_SYNTHESIS_MAX_ZONE_ITEMS] if str(item or "").strip()]


def _bounded_synthesis_context(request: FactSynthesisRequest) -> dict[str, list[str]]:
    return {
        "Active": _bounded_synthesis_zone(request.three_zone_context.Active),
        "Compress": _bounded_synthesis_zone(request.three_zone_context.Compress),
        "Frozen": _bounded_synthesis_zone(request.three_zone_context.Frozen),
    }


def _extract_json_object_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("local LLM response did not contain a JSON object")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, tuple):
        values = list(value)
    elif value:
        values = [value]
    else:
        values = []

    result: list[str] = []
    for item in values:
        if isinstance(item, (dict, list)):
            text = json.dumps(item, ensure_ascii=False, sort_keys=True)
        else:
            text = str(item)
        text = _synthesis_trim(text, 900)
        if text and text not in result:
            result.append(text)
    return result


def _normalize_linked_dependencies(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    dependencies: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        keyword = _synthesis_trim(
            item.get("target_core_insight_keyword")
            or item.get("target_keyword")
            or item.get("keyword")
            or "",
            240,
        )
        edge_type = str(item.get("edge_type") or "").strip().upper()
        if not keyword or edge_type not in _SEMANTIC_LINK_EDGE_TYPES:
            continue
        key = (keyword.casefold(), edge_type)
        if key in seen:
            continue
        seen.add(key)
        dependencies.append({"target_core_insight_keyword": keyword, "edge_type": edge_type})
    return dependencies[:10]


def _fact_synthesis_tags(request: FactSynthesisRequest, context: dict[str, list[str]]) -> list[str]:
    haystack = " ".join([request.project_name, *context["Active"], *context["Compress"], *context["Frozen"]])
    known_tags = [
        "FastAPI",
        "Qdrant",
        "Docker",
        "LangGraph",
        "Mem0",
        "LM Studio",
        "PowerShell",
        "Playwright",
        "WebSocket",
        "Python",
        "BHM",
    ]
    tags = [tag for tag in known_tags if re.search(re.escape(tag), haystack, re.IGNORECASE)]
    for tag in ("FactCrystal", "Crystallizer", request.project_name):
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:12]


def _fact_taxonomy_haystack(request: FactSynthesisRequest, context: dict[str, list[str]], payload: dict[str, Any]) -> str:
    payload_text = " ".join(
        str(value)
        for key, value in payload.items()
        if key not in {"linked_dependencies"} and value is not None
    )
    return " ".join([request.project_name, payload_text, *context["Active"], *context["Compress"], *context["Frozen"]]).lower()


def _normalize_fact_domain(value: Any, request: FactSynthesisRequest, context: dict[str, list[str]], payload: dict[str, Any]) -> FactCrystalDomain:
    candidate = str(value or "").strip().lower()
    allowed = {"frontend", "backend", "infra", "security", "product", "general"}
    if candidate in allowed:
        return candidate  # type: ignore[return-value]
    text = _fact_taxonomy_haystack(request, context, payload)
    if any(token in text for token in ("secret", "token", "auth", "permission", "vulnerab", "security", "hard-delete")):
        return "security"
    if any(token in text for token in ("ui", "interface", "color", "layout", "canvas", "three.js", "html", "css", "react", "screenshot")):
        return "frontend"
    if any(token in text for token in ("fastapi", "endpoint", "route", "api", "adapter", "service logic", "backend")):
        return "backend"
    if any(token in text for token in ("roadmap", "requirement", "acceptance criteria", "user workflow", "product")):
        return "product"
    if any(token in text for token in ("docker", "wsl", "powershell", "qdrant", "mem0", "mcp", "runtime", "worker", "port", "deploy")):
        return "infra"
    return "general"


def _normalize_fact_priority(value: Any, request: FactSynthesisRequest, context: dict[str, list[str]], payload: dict[str, Any]) -> FactCrystalPriority:
    candidate = str(value or "").strip().lower()
    aliases = {"normal": "medium", "trivial": "low"}
    candidate = aliases.get(candidate, candidate)
    if candidate in {"low", "medium", "high", "critical"}:
        return candidate  # type: ignore[return-value]
    text = _fact_taxonomy_haystack(request, context, payload)
    if any(token in text for token in ("data loss", "corrupt", "secret", "token", "fatal", "critical outage")):
        return "critical"
    if any(token in text for token in ("error", "crash", "failed", "failure", "timeout", "traceback", "exception", "regression", "broken validation")):
        return "high"
    return "medium"


def _normalize_fact_semantic_type(value: Any, request: FactSynthesisRequest, context: dict[str, list[str]], payload: dict[str, Any]) -> FactCrystalSemanticType:
    candidate = str(value or "").strip().lower()
    aliases = {"fact": "knowledge", "decision-log": "architecture", "requirement": "feature", "error": "bugfix", "log": "knowledge"}
    candidate = aliases.get(candidate, candidate)
    if candidate in {"architecture", "bugfix", "feature", "refactor", "knowledge"}:
        return candidate  # type: ignore[return-value]
    text = _fact_taxonomy_haystack(request, context, payload)
    if any(token in text for token in ("architecture", "contract", "adr", "decision", "cross-component")):
        return "architecture"
    if any(token in text for token in ("bug", "root cause", "regression", "error", "traceback", "exception", "failed", "failure")):
        return "bugfix"
    if any(token in text for token in ("feature", "capability", "implement", "delivery", "requirement")):
        return "feature"
    if any(token in text for token in ("refactor", "restructure", "cleanup", "migration")):
        return "refactor"
    return "knowledge"


def _normalize_fact_synthesis(raw: dict[str, Any], request: FactSynthesisRequest) -> dict[str, Any]:
    payload = raw.get("fact_crystal") if isinstance(raw.get("fact_crystal"), dict) else raw
    context = _bounded_synthesis_context(request)

    core_insight = (
        payload.get("core_insight")
        or payload.get("architecture_impact")
        or payload.get("problem_or_solution_entity")
        or f"{request.project_name} session {request.session_id} produced a reusable fact crystal."
    )
    root_cause = (
        payload.get("root_cause_resolved")
        or payload.get("root_cause")
        or "Three-zone session context was distilled into a long-term fact without replaying raw historical logs."
    )
    reusable_patterns = _string_list(payload.get("reusable_patterns") or payload.get("durable_guidance"))
    if not reusable_patterns:
        reusable_patterns = [
            "Send Active/Compress/Frozen session context to backend synthesis before memory upsert.",
            "Preserve raw hot details only in Active; use signatures and memory IDs for older context.",
        ]
    tags = _string_list(payload.get("tags") or payload.get("taxonomy_tags"))
    if not tags:
        tags = _fact_synthesis_tags(request, context)
    importance_score = normalize_importance_score(payload.get("importance_score") or request.importance_score)
    linked_dependencies = _normalize_linked_dependencies(payload.get("linked_dependencies"))
    fact = FactCrystal(
        core_insight=_synthesis_trim(core_insight, 1200),
        root_cause_resolved=_synthesis_trim(root_cause, 1200),
        reusable_patterns=reusable_patterns[:10],
        tags=tags[:12],
        importance_score=importance_score,
        linked_dependencies=linked_dependencies,
        domain=_normalize_fact_domain(payload.get("domain"), request, context, payload),
        priority=_normalize_fact_priority(payload.get("priority"), request, context, payload),
        semantic_type=_normalize_fact_semantic_type(payload.get("semantic_type"), request, context, payload),
    )

    return fact.model_dump()


def _fallback_fact_synthesis(request: FactSynthesisRequest, reason: str) -> dict[str, Any]:
    context = _bounded_synthesis_context(request)
    active_count = len(context["Active"])
    compress_count = len(context["Compress"])
    frozen_count = len(context["Frozen"])
    reason_text = _synthesis_trim(reason, 500) or "local LLM synthesis unavailable"
    return {
        "core_insight": (
            f"{request.project_name} session {request.session_id} was condensed from "
            f"{active_count} Active, {compress_count} Compress, and {frozen_count} Frozen context entries."
        ),
        "root_cause_resolved": (
            "Dynamic LLM synthesis was unavailable or returned invalid JSON; the backend produced a safe "
            f"structured fallback instead of failing the crystallizer. reason: {reason_text}"
        ),
        "reusable_patterns": [
            "Use backend synthesis as the crystallizer boundary and keep worker persistence on /bhm/memory/upsert.",
            "Treat invalid local LLM JSON as recoverable and return a schema-valid Fact Crystal.",
            "Keep historical context as memory IDs and distilled conclusions instead of raw transcript replay.",
        ],
        "tags": _fact_synthesis_tags(request, context),
        "importance_score": normalize_importance_score(request.importance_score),
        "linked_dependencies": [],
        "domain": "general",
        "priority": "medium",
        "semantic_type": "knowledge",
    }


def _format_fact_synthesis_crystal(fact: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Fact Crystal",
            f"core_insight: {_synthesis_trim(fact.get('core_insight'), 1200)}",
            f"root_cause_resolved: {_synthesis_trim(fact.get('root_cause_resolved'), 1200)}",
            f"reusable_patterns: {', '.join(_string_list(fact.get('reusable_patterns')))}",
            f"tags: {', '.join(_string_list(fact.get('tags')))}",
            f"importance_score: {normalize_importance_score(fact.get('importance_score'))}",
            f"domain: {fact.get('domain') or 'not set'}",
            f"priority: {fact.get('priority') or 'not set'}",
            f"semantic_type: {fact.get('semantic_type') or 'not set'}",
        ]
    )


async def _call_fact_synthesis_llm(request: FactSynthesisRequest) -> tuple[dict[str, Any], dict[str, int]]:
    context = _bounded_synthesis_context(request)
    user_payload = {
        "project_name": request.project_name,
        "session_id": request.session_id,
        "three_zone_context": context,
        "required_json_contract": {
            "core_insight": "string",
            "root_cause_resolved": "string",
            "reusable_patterns": ["string"],
            "tags": ["string"],
            "importance_score": "integer 1-10, optional; omit to use 5",
            "domain": "required; one of frontend, backend, infra, security, product, general",
            "priority": "required; one of low, medium, high, critical",
            "semantic_type": "required; one of architecture, bugfix, feature, refactor, knowledge",
            "linked_dependencies": [
                {
                    "target_core_insight_keyword": "string keyword for an older related crystal, optional",
                    "edge_type": "one of DEPENDS_ON, UPGRADES, CONTRADICTS",
                }
            ],
        },
        "output_rule": "Return only one strict JSON object. Do not wrap it in Markdown.",
    }
    gateway = LocalLLMGateway(
        prompts=PromptRegistry(
            [PromptDefinition("fact-synthesis", "1", FACT_SYNTHESIS_SYSTEM_PROMPT, output_mode="json")]
        ),
        models=ModelRegistry(
            [
                ModelDefinition(
                    settings.mem0_llm_model,
                    settings.mem0_openai_base_url,
                    frozenset({"text", "json"}),
                    api_key=settings.mem0_api_key,
                )
            ]
        ),
        adapter=LocalOpenAICompatibleAdapter(),
    )
    gateway_request = GatewayRequest(
        request_id=f"fact-synthesis-{uuid.uuid4().hex}",
        prompt_id="fact-synthesis",
        model_id=settings.mem0_llm_model,
        messages=(
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ),
        max_tokens=_FACT_SYNTHESIS_MAX_TOKENS,
        temperature=0.0,
        json_required_keys=("core_insight", "root_cause_resolved", "reusable_patterns", "tags"),
        timeout_seconds=_FACT_SYNTHESIS_TIMEOUT_SECONDS,
    )
    async with httpx.AsyncClient(timeout=_FACT_SYNTHESIS_TIMEOUT_SECONDS) as client:
        async def transport(url, payload, headers, timeout):
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("local LLM gateway expected JSON object")
            return value

        result = await gateway.acomplete_with_transport(gateway_request, transport)
    if not result.ok:
        failure = result.failure or {"code": "gateway_failure", "message": "unknown gateway failure"}
        raise ValueError(f"local LLM gateway {failure.get('code')}: {failure.get('message')}")
    content = result.content.strip()
    usage = result.usage
    fact = _normalize_fact_synthesis(_extract_json_object_from_text(content), request)
    tokens = {
        "prompt": int(usage.get("prompt_tokens") or 0),
        "completion": int(usage.get("completion_tokens") or 0),
        "total": int(usage.get("total_tokens") or 0),
    }
    return fact, tokens


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/favicon.ico", response_class=FileResponse)
def favicon_ico() -> FileResponse:
    return FileResponse(STATIC_DIR / "bhm-favicon.svg", media_type="image/svg+xml")


@app.get("/favicon.svg", response_class=FileResponse)
def favicon_svg() -> FileResponse:
    return FileResponse(STATIC_DIR / "bhm-favicon.svg", media_type="image/svg+xml")


@app.get("/static/redoc.standalone.js", response_class=FileResponse)
def redoc_bundle() -> FileResponse:
    return FileResponse(STATIC_DIR / "redoc.standalone.js", media_type="application/javascript")


@app.get("/static/3d-force-graph.min.js", response_class=FileResponse)
def force_graph_bundle() -> FileResponse:
    return FileResponse(STATIC_DIR / "3d-force-graph.min.js", media_type="application/javascript")


@app.get("/static/three.module.min.js", response_class=FileResponse)
def three_module_bundle() -> FileResponse:
    return FileResponse(STATIC_DIR / "three.module.min.js", media_type="application/javascript")


@app.get("/static/three.core.min.js", response_class=FileResponse)
def three_core_bundle() -> FileResponse:
    return FileResponse(STATIC_DIR / "three.core.min.js", media_type="application/javascript")


@app.get("/redoc")
def local_redoc() -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html>
<head>
  <title>{settings.app_name} - ReDoc</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <style>
    body {{
      margin: 0;
      padding: 0;
      background: #05070f;
    }}
  </style>
</head>
<body>
  <noscript>ReDoc requires Javascript to function.</noscript>
  <redoc spec-url="/openapi.json"></redoc>
  <script src="/static/redoc.standalone.js"></script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/health/live")
def health_live() -> dict:
    return health_live_payload(service=settings.app_name, environment=settings.app_env)


@app.get("/health/dependencies")
def health_dependencies() -> dict:
    return dependency_report(include_optional=True)


@app.get("/health/ready")
def health_ready() -> dict:
    report = dependency_report()
    storage = storage_runtime_state()
    memory_store = _memory_store_state()
    fallback_active = _fallback_grace_active()
    return health_ready_payload(
        dependency_report=report,
        storage=storage.as_dict(),
        memory_store=memory_store.as_dict(),
        fallback_mode=_configured_fallback_mode(),
        fallback_active=fallback_active,
        mem0_plan=mem0_runtime_plan(),
        provider_warmup=_get_provider_warmup_status(),
    )


@app.get("/bhm/health")
def bhm_health() -> dict:
    storage = storage_runtime_state()
    memory_store = _memory_store_state()
    fallback_active = _fallback_grace_active()
    return bhm_health_payload(
        service=settings.app_name,
        version=RUNTIME_VERSION,
        port=settings.port,
        transport=_MCP_STREAMABLE_HTTP.contract_snapshot()["sessions"],
        storage=storage.as_dict(),
        memory_store=memory_store.as_dict(),
        fallback_mode=_configured_fallback_mode(),
        fallback_active=fallback_active,
    )


@app.get("/bhm/infra/boot-report")
def bhm_infra_boot_report() -> dict:
    return _read_boot_report()


@app.get("/bhm/ui/boot-report")
def bhm_ui_boot_report() -> dict[str, Any]:
    """Return the redacted boot passport allowed to an authenticated UI session."""

    report = _read_boot_report()
    allowed = {"status", "elapsed_seconds", "qdrant", "lm_studio", "timestamp"}
    return {key: report[key] for key in allowed if key in report}


def _llm_governor() -> LLMResourceGovernor:
    global _LLM_GOVERNOR
    with _LLM_GOVERNOR_LOCK:
        if _LLM_GOVERNOR is None:
            _LLM_GOVERNOR = LLMResourceGovernor(GovernorConfig.from_env())
        return _LLM_GOVERNOR


def _llm_public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "job_type": job.get("job_type"),
        "project": job.get("project"),
        "priority": job.get("priority"),
        "status": job.get("status"),
        "attempts": job.get("attempts"),
        "max_attempts": job.get("max_attempts"),
        "available_at": job.get("available_at"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "last_error": redact_secret_text(str(job.get("last_error") or "")).value[:1000] or None,
        "checkpoint_digest": job.get("checkpoint_digest"),
        "result_available": job.get("result") is not None,
    }


@app.get("/bhm/llm/capabilities")
def bhm_llm_capabilities() -> dict[str, Any]:
    """Expose the bounded Codex/plugin delegation contract without starting execution."""

    queue_info: dict[str, Any] = {
        "schema_version": LLM_JOB_QUEUE_SCHEMA_VERSION,
        "path": str(_LLM_JOB_QUEUE.path),
        "capacity": _LLM_JOB_QUEUE.capacity,
        "exists": _LLM_JOB_QUEUE.path.exists(),
    }
    if queue_info["exists"]:
        try:
            queue_info.update(_LLM_JOB_QUEUE.status())
        except LLMJobQueueError as exc:
            queue_info["available"] = False
            queue_info["error"] = redact_secret_text(str(exc)).value[:500]
    try:
        governor_info = _llm_governor().status()
    except LLMResourceGovernorError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_governor_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return {
        "schema_version": "bhm.llm.delegation.v1",
        "gateway_schema": GATEWAY_SCHEMA_VERSION,
        "queue_schema": LLM_JOB_QUEUE_SCHEMA_VERSION,
        "safety_policy": LLM_SAFETY_POLICY_VERSION,
        "queue": queue_info,
        "long_task": {
            "schema_version": LLM_LONG_TASK_PLAN_VERSION,
            "store": _LLM_LONG_TASK_STORE.status(),
            "map_reduce": True,
            "max_chunks": LLM_LONG_TASK_MAX_CHUNKS,
            "max_fanout": LLM_LONG_TASK_MAX_FANOUT,
            "checkpoint_resume": True,
            "cache": True,
            "execution_enabled": False,
        },
        "multi_candidate": {
            "schema_version": LLM_CANDIDATE_SCHEMA_VERSION,
            "judge_version": LLM_CANDIDATE_JUDGE_VERSION,
            "roles": list(LLM_CANDIDATE_ROLES),
            "max_candidates": LLM_CANDIDATE_MAX,
            "evidence_first": True,
            "consensus_is_correctness": False,
            "execution_enabled": False,
        },
        "safe_patch": {
            "schema_version": SAFE_PATCH_SCHEMA_VERSION,
            "quarantine": True,
            "ast_context": True,
            "sandbox": True,
            "max_files": SAFE_PATCH_MAX_FILES,
            "max_diff_bytes": SAFE_PATCH_MAX_DIFF_BYTES,
            "max_timeout_seconds": SAFE_PATCH_MAX_TIMEOUT_SECONDS,
            "approval_required": True,
            "apply_enabled": False,
            "commit_enabled": False,
        },
        "local_first_policy": {
            **delegation_policy_snapshot(),
            "schema_version": LLM_DELEGATION_POLICY_VERSION,
        },
        "memory_foundry": {
            "schema_version": MEMORY_FOUNDRY_SCHEMA_VERSION,
            "preview_only": True,
            "fact_crystals": True,
            "super_crystal": True,
            "duplicate_conflict_relation_proposals": True,
            "stale_review": True,
            "cross_project_patterns": True,
            "digest_verification": True,
            "undo_window": True,
            "writes_performed": False,
            "auto_apply": False,
        },
        "retrieval_lab": {
            "schema_version": RETRIEVAL_LAB_SCHEMA_VERSION,
            "features": list(RETRIEVAL_LAB_FEATURES),
            "query_rewrite": True,
            "multi_query": True,
            "hyde": True,
            "deterministic_rerank": True,
            "hard_negatives": True,
            "synthetic_benchmark": True,
            "failure_cases": True,
            "filter_gate": True,
            "latency_gate": True,
            "leakage_gate": True,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "repository_intelligence": {
            "schema_version": REPOSITORY_INTELLIGENCE_SCHEMA_VERSION,
            "max_files": REPOSITORY_INTELLIGENCE_MAX_FILES,
            "file_symbol_summaries": True,
            "architectural_map": True,
            "dependency_change_impact": True,
            "test_selection_hints": True,
            "technical_debt": True,
            "issue_clustering": True,
            "source_refs": True,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "qa_incident_factory": {
            "schema_version": QA_INCIDENT_SCHEMA_VERSION,
            "features": list(QA_INCIDENT_FEATURES),
            "unit_property_fuzz_adversarial_drafts": True,
            "log_trace_clustering": True,
            "root_cause_hypotheses": True,
            "regression_triage": True,
            "release_security_candidates": True,
            "deterministic_verdicts": True,
            "evidence_required": True,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "documentation_factory": {
            "schema_version": DOCUMENTATION_FACTORY_SCHEMA_VERSION,
            "features": list(DOCUMENTATION_FACTORY_FEATURES),
            "readme_adr_changelog_release_runbook_migration": True,
            "localization": True,
            "vision_requires_confirmation": True,
            "patch_outputs": True,
            "link_section_secret_gates": True,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "night_shift": {
            "schema_version": NIGHT_SHIFT_SCHEMA_VERSION,
            "safe_job_types": list(NIGHT_SHIFT_SAFE_JOB_TYPES),
            "dry_run_default": True,
            "automatic_pause_on_user_activity": True,
            "automatic_pause_on_vram_temperature": True,
            "morning_report": True,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "model_router": {
            "schema_version": MODEL_ROUTER_SCHEMA_VERSION,
            "capabilities": list(MODEL_ROUTER_CAPABILITIES),
            "context_profiles": list(MODEL_ROUTER_CONTEXT_PROFILES),
            "measured_profile_required": True,
            "local_only_required": True,
            "cloud_fallback": False,
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "cache": {
            "schema_version": LLM_CACHE_POLICY_VERSION,
            "store": _LLM_CACHE_STORE.status(),
            "max_entries": LLM_CACHE_MAX_ENTRIES,
            "ttl_seconds": LLM_CACHE_DEFAULT_TTL_SECONDS,
            "key_fields": [
                "project",
                "content_digest",
                "prompt_version",
                "model_digest",
                "parameters_digest",
            ],
            "exact_result_reuse": True,
            "prefix_reuse": True,
            "invalidation": True,
            "privacy_boundary": {
                "raw_content_stored": False,
                "raw_prompt_stored": False,
                "raw_prefix_stored": False,
                "cross_project_isolation": True,
                "secret_or_injection_inputs_blocked": True,
            },
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "learning": {
            "schema_version": LLM_LEARNING_POLICY_VERSION,
            "store": _LLM_LEARNING_STORE.status(),
            "max_records": LLM_LEARNING_MAX_RECORDS,
            "reviewed_only": True,
            "accepted_to_eval": True,
            "accepted_to_few_shot": True,
            "rejected_to_regression": True,
            "unverified_outputs_excluded": True,
            "raw_values_stored": False,
            "training": {
                "lora_enabled": False,
                "qlora_enabled": False,
                "training_started": False,
                "eligible": False,
                "requires_curated_dataset": True,
                "requires_baseline": True,
            },
            "execution_enabled": False,
            "writes_performed": False,
            "auto_apply": False,
        },
        "governor": governor_info,
        "capabilities": {
            "submit": True,
            "status": True,
            "result": True,
            "cancel": True,
        },
        "execution_enabled": False,
        "mcp_core_tools": len(CORE_TOOL_NAMES),
        "authority": PROPOSAL_AUTHORITY,
        "auto_apply": False,
        "requires_approval": True,
    }


@app.post("/bhm/llm/learning/review")
def bhm_llm_learning_review(request: LLMLearningReviewRequest) -> dict[str, Any]:
    """Persist one explicit reviewed outcome; never start training or apply output."""

    try:
        return _LLM_LEARNING_STORE.record_review(
            project=request.project,
            source_job_id=request.source_job_id,
            decision=request.decision,
            reviewer=request.reviewer,
            review_reason=request.review_reason,
            input_value=request.input,
            prompt=request.prompt,
            output=request.output,
            prompt_version=request.prompt_version,
            model_digest=request.model_digest,
            parameters=request.parameters,
            validation=request.validation,
            provenance=request.provenance,
        )
    except LLMLearningCollision as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "llm_learning_review_collision", "source_job_id": exc.source_job_id},
        ) from exc
    except LLMLearningStoreFull as exc:
        raise HTTPException(
            status_code=429,
            detail={"error": "llm_learning_store_full", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "30"},
        ) from exc
    except (LLMLearningPrivacyError, LLMLearningReviewError, LLMLearningBoundsError, LLMLearningError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "llm_learning_review_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc


@app.get("/bhm/llm/learning")
def bhm_llm_learning_status(project: str | None = None) -> dict[str, Any]:
    """Expose reviewed-learning counters and the fail-closed training gate."""

    try:
        return _LLM_LEARNING_STORE.status(project=project)
    except LLMLearningError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_learning_store_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc


@app.post("/bhm/llm/learning/curate")
def bhm_llm_learning_curate(request: LLMLearningCurateRequest) -> dict[str, Any]:
    """Build a reviewed eval/few-shot/regression dataset proposal only."""

    try:
        return _LLM_LEARNING_STORE.curate_dataset(
            project=request.project,
            limit=request.limit,
            include_payload=request.include_payload,
        )
    except LLMLearningError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "llm_learning_curation_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc


@app.post("/bhm/llm/candidates/plan")
def bhm_llm_candidate_plan(request: LLMCandidatePlanRequest) -> dict[str, Any]:
    """Build an evidence-first candidate plan without starting model execution."""

    try:
        plan = build_candidate_plan(
            request.task_id,
            request.objective,
            project=request.project,
            roles=request.roles,
            candidate_count=request.candidate_count,
            prompt_version=request.prompt_version,
            model_digest=request.model_digest,
        )
    except CandidateError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "llm_candidate_plan_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return plan.as_dict()


@app.post("/bhm/llm/policy/decide")
def bhm_llm_delegation_decide(request: LLMDelegationDecisionRequest) -> dict[str, Any]:
    """Return a deterministic local/Codex/operator escalation decision."""

    try:
        decision = decide_delegation(
            request.task_type,
            confidence=request.confidence,
            sensitivity=request.sensitivity,
            mutation_requested=request.mutation_requested,
            evidence_count=request.evidence_count,
            local_capabilities=request.local_capabilities,
            risk_flags=request.risk_flags,
            operator_approved=request.operator_approved,
        )
    except DelegationPolicyError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "llm_delegation_policy_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return decision.as_dict()


@app.post("/bhm/llm/memory-foundry/preview")
def bhm_llm_memory_foundry_preview(request: MemoryFoundryPreviewRequest) -> dict[str, Any]:
    """Compose bounded memory-consolidation proposals without performing writes."""

    all_records = _load_live_memories()
    records = [
        item
        for item in all_records
        if _memory_matches_filters(item, project=request.project, include_archived=False)
    ]
    if request.memory_ids is not None:
        selected = {str(item) for item in request.memory_ids}
        records = [item for item in records if str(item.get("source_id") or "") in selected]
    records = sorted(
        records,
        key=lambda item: (str(item.get("updated_at") or item.get("created_at") or ""), str(item.get("source_id") or "")),
        reverse=True,
    )[:MEMORY_FOUNDRY_MAX_RECORDS]
    detection_request = MemoryDetectRequest(
        project=request.project,
        limit=min(max(request.limit * 2, 20), 200),
        include_archived=False,
    )
    try:
        preview = build_memory_foundry_preview(
            records,
            project=_canonical_project(request.project),
            cross_project_records=all_records if request.memory_ids is None else records,
            duplicate_candidates=_detect_duplicates(detection_request),
            conflict_candidates=_detect_conflicts(detection_request),
            relation_candidates=_relation_suggest(
                RelationSuggestRequest(project=request.project, limit=detection_request.limit)
            ).get("suggestions", []),
            stale_days=request.stale_days,
            undo_window_seconds=request.undo_window_seconds,
            limit=request.limit,
        )
    except MemoryFoundryError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "memory_foundry_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


@app.post("/bhm/llm/retrieval-lab/preview")
async def bhm_llm_retrieval_lab_preview(request: RetrievalLabPreviewRequest) -> dict[str, Any]:
    """Build retrieval experiments and gates without mutating the live path."""

    candidates = request.candidates
    observed_latency_ms: float | None = None
    if candidates is None and request.use_live_candidates:
        started = time.perf_counter()
        try:
            await _ensure_provider_warmup_ready()
            hits, _total = await federated_search(
                request.query,
                _canonical_project(request.project),
                limit=min(max(request.limit * 4, 20), 200),
            )
            candidates = hits
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "retrieval_lab_source_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            ) from exc
        finally:
            observed_latency_ms = (time.perf_counter() - started) * 1000.0
    if candidates is None:
        candidates = []
    try:
        preview = build_retrieval_lab_preview(
            request.query,
            project=_canonical_project(request.project),
            candidates=candidates,
            feature_flags=request.feature_flags,
            limit=request.limit,
            benchmark_cases=request.benchmark_cases,
            latency_budget_ms=request.latency_budget_ms,
            observed_latency_ms=observed_latency_ms,
        )
    except RetrievalLabError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "retrieval_lab_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


@app.post("/bhm/llm/repository-intelligence/preview")
def bhm_llm_repository_intelligence_preview(request: RepositoryIntelligencePreviewRequest) -> dict[str, Any]:
    """Analyze a bounded repository snapshot without writing source or Git state."""

    source_files = request.files
    if source_files is None:
        base = Path(settings.repo_root).resolve()
        candidate_root = _resolve_bounded_repository_root(request.root, base)
        try:
            source_files = collect_repository_files(candidate_root, request.paths)
        except RepositoryIntelligenceError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "repository_intelligence_source_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
            ) from exc
    try:
        preview = build_repository_intelligence_preview(
            source_files or [],
            project=request.project,
            changed_paths=request.changed_paths,
            include_tests=request.include_tests,
            max_files=request.max_files,
        )
    except RepositoryIntelligenceError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "repository_intelligence_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


def _code_graph_query_root_id(project: str, root: str | Path) -> str:
    return str(probe_repository_state(Path(root), project=project).root_id)


def _code_graph_query_response(request: CodeGraphQueryRequest, *, explain: bool) -> dict[str, Any]:
    project = _canonical_project(request.project)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    root_id = request.root_id or _code_graph_query_root_id(project, settings.repo_root)
    function = explain_code_graph if explain else query_code_graph
    try:
        return function(
            database_path,
            project=project,
            root_id=root_id,
            operation=request.operation,
            query=request.query,
            depth=request.depth,
            limit=request.limit,
            max_tokens=request.max_tokens,
            time_budget_ms=request.time_budget_ms,
            snapshot_id=request.snapshot_id,
        )
    except CodeGraphQueryError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        status_code = 503 if "unavailable" in detail.casefold() else 422
        raise HTTPException(status_code=status_code, detail={"error": "code_graph_query_rejected", "detail": detail}) from exc


@app.post("/bhm/code-graph/query", include_in_schema=False)
def bhm_code_graph_query(request: CodeGraphQueryRequest) -> dict[str, Any]:
    """Internal bounded graph query; public MCP catalog remains unchanged."""

    return _code_graph_query_response(request, explain=False)


@app.post("/bhm/code-graph/explain", include_in_schema=False)
def bhm_code_graph_explain(request: CodeGraphQueryRequest) -> dict[str, Any]:
    """Internal bounded graph explain path with provenance evidence."""

    return _code_graph_query_response(request, explain=True)


_PUBLIC_CODE_TOOL_OPERATIONS = frozenset(
    {"index", "status", "projects", "watch", "search", "code_search", "code_snippet", "graph_artifact_export", "graph_artifact_verify", "graph_artifact_promotion_plan", "graph_query", "graph", "schema", "coverage", "architecture", "trace", "trace_evidence", "impact", "impact_preview", "cross_repo", "package_resolution", "dependency_provenance", "type_references", "bicep_module_resolution"}
)


def _public_code_contract_digest() -> str:
    """Return the deterministic digest for the public code-tools contract.

    The digest binds the single ``bhm`` surface to its versioned graph schema,
    parser registry, operation allowlist and execution boundary.  It is a
    contract identity, not a repository/source digest.
    """

    payload = {
        "schema_version": "bhm.public-code-tools.v1",
        "graph_schema_version": CODE_GRAPH_SCHEMA_VERSION,
        "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION,
        "parser_registry_digest": PARSER_REGISTRY_DIGEST,
        "language_inventory_digest": LANGUAGE_INVENTORY_DIGEST,
        "operations": sorted(_PUBLIC_CODE_TOOL_OPERATIONS),
        "watcher": {
            "schema_version": "bhm.repository-watch-backpressure.v1",
            "default_max_inflight_jobs": DEFAULT_WATCH_MAX_INFLIGHT_JOBS,
            "max_inflight_jobs": MAX_WATCH_MAX_INFLIGHT_JOBS,
            "operator_managed": True,
            "starts_background_daemon": False,
        },
        "execution": {
            "raw_source_returned": False,
            "autonomous_apply": False,
            "arbitrary_sql": False,
            "second_namespace": False,
        },
        "code_search": {
            "semantic_fusion": {
                "readiness_gate": {
                    "schema_version": "bhm.semantic-readiness.v1",
                    "feature_flag": "BHM_SEMANTIC_READINESS_GATE",
                    "explicit_operator_mode": "-SemanticFusion",
                    "provider_called_on_not_ready": False,
                    "model_started_on_not_ready": False,
                    "writes_on_not_ready": False,
                    "new_mcp_namespace": False,
                }
            },
            "semantic_query": {
                "schema_version": "bhm.code-graph.semantic-query.v1",
                "input": "array<string>",
                "max_terms": 32,
                "max_term_chars": 160,
                "result_stream": "semantic_results",
                "algorithm": "hashing-metadata-v1",
                "authority": "sqlite-authoritative-code-graph",
                "raw_source_returned": False,
                "vectors_returned": False,
                "network_called": False,
                "model_started": False,
                "writes_qdrant": False,
            },
        },
        "index": {
            "force_refresh": {
                "type": "boolean",
                "default": False,
                "requires_apply": True,
                "operator_epoch_bound": True,
                "creates_new_snapshot_on_unchanged_content": True,
            },
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _public_code_embedding_contract() -> dict[str, Any]:
    """Return safe, reproducible embedding provenance for code-search fusion."""

    model = str(settings.mem0_embedding_model or "").strip()[:160]
    model_digest = hashlib.sha256(model.encode("utf-8")).hexdigest() if model else ""
    try:
        dimensions = max(int(settings.mem0_embedding_dims), 0)
    except (TypeError, ValueError):
        dimensions = 0
    return {
        "schema_version": "bhm.code-search.embedding-contract.v1",
        "provider": "mem0-qdrant-projection",
        "model": model,
        "model_digest": model_digest,
        "dimensions": dimensions,
        "feature_flag": "BHM_CODE_SEMANTIC_FUSION",
        "feature_enabled": semantic_fusion_enabled(),
        "authority": "qdrant-projection-only",
        "writes_sqlite_state": False,
        "writes_qdrant": False,
        "raw_source_returned": False,
    }


def _resolve_public_code_root(raw_root: str) -> Path:
    """Resolve a repository root inside the operator-approved repos boundary."""

    # lgtm [py/path-injection]
    repos_root = settings.repo_root.resolve().parent
    candidate = _resolve_bounded_repository_root(raw_root, repos_root)
    relative = candidate.relative_to(repos_root)
    blocked = {".src", "runtime", ".env", "secrets", "credentials", "private-keys", "private_keys"}
    # lgtm [py/path-injection]
    if any(part.casefold() in blocked for part in relative.parts) or not candidate.is_dir():
        raise HTTPException(status_code=422, detail={"error": "repository_root_rejected"})
    return candidate


def _resolve_bounded_repository_root(raw_root: str, base: Path) -> Path:
    """Resolve a request root only after a normalized containment check."""

    raw = str(raw_root or ".").strip()
    if not raw or len(raw) > 512 or "\x00" in raw:
        raise HTTPException(status_code=422, detail={"error": "repository_root_rejected"})
    base_name = os.path.realpath(os.fspath(base))
    expanded = os.path.expanduser(raw)
    candidate_name = os.path.realpath(expanded if os.path.isabs(expanded) else os.path.join(base_name, expanded))
    try:
        contained = os.path.commonpath((base_name, candidate_name)) == base_name
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "repository_root_outside_allowlist"}) from exc
    if not contained:
        raise HTTPException(status_code=422, detail={"error": "repository_root_outside_allowlist"})
    candidate = Path(candidate_name)
    # lgtm [py/path-injection]
    if not candidate.is_dir():
        raise HTTPException(status_code=422, detail={"error": "repository_root_rejected"})
    return candidate


def _public_code_root_id(project: str, root: Path) -> str:
    return str(probe_repository_state(root, project=project).root_id)


def _public_code_projects(database_path: Path) -> dict[str, Any]:
    store = SQLiteRepositoryIndexStore(database_path)
    schema = store.inspect_schema()
    if not schema.get("ready"):
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "projects": [],
            "count": 0,
            "repository_schema": schema,
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
        }
    connection = store._connect(read_only=True)  # bounded read-only catalog helper
    try:
        rows = connection.execute(
            """
            SELECT current.project, current.root_id, current.snapshot_id,
                   snapshots.root_path, snapshots.state_digest, snapshots.snapshot_digest,
                   snapshots.git_head, snapshots.dirty, snapshots.completed_at
            FROM repository_index_current AS current
            JOIN repository_index_snapshots AS snapshots
              ON snapshots.snapshot_id = current.snapshot_id
            ORDER BY current.project, current.root_id
            """
        ).fetchall()
        projects = []
        for row in rows:
            item = dict(row)
            item["dirty"] = bool(item.get("dirty"))
            projects.append(item)
    finally:
        connection.close()
    return {
        "schema_version": "bhm.public-code-tools.v1",
        "projects": projects,
        "count": len(projects),
        "repository_schema": schema,
        "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
    }


@app.post("/bhm/code-tools", include_in_schema=False)
async def bhm_public_code_tools(request: PublicCodeToolRequest) -> dict[str, Any]:
    """Unified public MCP code-tools contract with bounded provenance.

    This route is intentionally not an arbitrary graph language endpoint.  It
    dispatches only the fixed operations in ``_PUBLIC_CODE_TOOL_OPERATIONS``;
    all graph traversal remains allowlisted and budgeted by ``query_code_graph``.
    """

    operation = str(request.operation).strip().casefold()
    if operation not in _PUBLIC_CODE_TOOL_OPERATIONS:
        raise HTTPException(status_code=422, detail={"error": "code_tool_operation_rejected"})
    if request.force_refresh and operation != "index":
        raise HTTPException(
            status_code=422,
            detail={"error": "force_refresh_index_only"},
        )
    if request.force_refresh and not request.build_graph:
        raise HTTPException(
            status_code=422,
            detail={"error": "force_refresh_requires_build_graph"},
        )
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    if operation == "projects":
        return {**_public_code_projects(database_path), "contract_digest": _public_code_contract_digest()}
    if operation == "cross_repo":
        try:
            return {
                **build_cross_repo_link_preview(database_path, limit=request.limit, project=request.project),
                "operation": operation,
                "contract_digest": _public_code_contract_digest(),
            }
        except (CodeGraphError, OSError, sqlite3.Error) as exc:
            raise HTTPException(status_code=503, detail={"error": "cross_repo_preview_unavailable", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
    root = _resolve_public_code_root(request.root)
    project = _canonical_project(request.project)
    root_id = _public_code_root_id(project, root)
    if operation == "package_resolution":
        package_snapshot: Mapping[str, Any] = {}
        try:
            snapshot_probe = SQLiteCodeGraphStore(database_path).current_snapshot(project, root_id, include_material=False)
            if isinstance(snapshot_probe, Mapping):
                package_snapshot = snapshot_probe
        except (CodeGraphError, OSError, sqlite3.Error):
            package_snapshot = {}
        if request.expected_graph_digest and not package_snapshot:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        package_graph_digest = str(package_snapshot.get("graph_digest") or "")
        if request.expected_graph_digest and request.expected_graph_digest != package_graph_digest:
            raise HTTPException(status_code=409, detail={"error": "expected_graph_digest_mismatch", "expected_graph_digest": request.expected_graph_digest, "actual_graph_digest": package_graph_digest})
        try:
            result = resolve_package_manifests(root, limit=min(int(request.limit), 64))
        except PackageResolutionError as exc:
            raise HTTPException(status_code=422, detail={"error": "package_resolution_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        result["resolution_receipt"] = build_package_resolution_receipt(result)
        result["resolution_quality"] = build_resolution_quality_receipt(
            package_result=result,
            graph_snapshot_id=str(package_snapshot.get("graph_snapshot_id") or ""),
            graph_digest=package_graph_digest,
            snapshot_digest=str(package_snapshot.get("snapshot_digest") or ""),
            repository_snapshot_id=str(package_snapshot.get("repository_snapshot_id") or ""),
            language_inventory_digest=LANGUAGE_INVENTORY_DIGEST,
            parser_registry_digest=PARSER_REGISTRY_DIGEST,
            contract_digest=_public_code_contract_digest(),
            expected_graph_digest=str(request.expected_graph_digest or ""),
        )
        return {
            "schema_version": PACKAGE_RESOLUTION_SCHEMA_VERSION,
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "contract_digest": _public_code_contract_digest(),
            **result,
        }
    if operation == "dependency_provenance":
        try:
            result = resolve_dependency_provenance(root, limit=min(int(request.limit), 64))
        except DependencyProvenanceError as exc:
            raise HTTPException(status_code=422, detail={"error": "dependency_provenance_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        current_snapshot: Mapping[str, Any] = {}
        try:
            snapshot_probe = SQLiteCodeGraphStore(database_path).current_snapshot(project, root_id, include_material=False)
            if isinstance(snapshot_probe, Mapping):
                current_snapshot = snapshot_probe
        except (CodeGraphError, OSError, sqlite3.Error):
            current_snapshot = {}
        dependency_graph_digest = str(current_snapshot.get("graph_digest") or "")
        if request.expected_graph_digest and request.expected_graph_digest != dependency_graph_digest:
            raise HTTPException(status_code=409, detail={"error": "expected_graph_digest_mismatch", "expected_graph_digest": request.expected_graph_digest, "actual_graph_digest": dependency_graph_digest})
        slo_payload = bhm_health_slo()
        slo_status = str(slo_payload.get("status") or "unknown") if isinstance(slo_payload, Mapping) else "unknown"
        result["quality_receipt"] = build_dependency_provenance_receipt(
            result,
            graph_snapshot_id=str(current_snapshot.get("graph_snapshot_id") or ""),
            graph_digest=str(current_snapshot.get("graph_digest") or ""),
            runtime_slo_status=slo_status,
            snapshot_digest=str(current_snapshot.get("snapshot_digest") or ""),
        )
        result["resolution_quality"] = build_resolution_quality_receipt(
            dependency_result=result,
            graph_snapshot_id=str(current_snapshot.get("graph_snapshot_id") or ""),
            graph_digest=dependency_graph_digest,
            snapshot_digest=str(current_snapshot.get("snapshot_digest") or ""),
            repository_snapshot_id=str(current_snapshot.get("repository_snapshot_id") or ""),
            parser_registry_digest=PARSER_REGISTRY_DIGEST,
            language_inventory_digest=LANGUAGE_INVENTORY_DIGEST,
            contract_digest=_public_code_contract_digest(),
            expected_graph_digest=str(request.expected_graph_digest or ""),
            runtime_slo_status=slo_status,
        )
        return {
            "schema_version": DEPENDENCY_PROVENANCE_SCHEMA_VERSION,
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "contract_digest": _public_code_contract_digest(),
            **result,
        }
    if operation == "graph_query":
        try:
            return {
                **query_graph_dsl(
                    str(database_path),
                    project=project,
                    root_id=root_id,
                    query=request.query,
                    limit=request.limit,
                    offset=request.offset,
                    time_budget_ms=request.time_budget_ms,
                ),
                "contract_digest": _public_code_contract_digest(),
            }
        except GraphDslError as exc:
            status_code = 503 if "unavailable" in str(exc).casefold() else 422
            raise HTTPException(status_code=status_code, detail={"error": "graph_query_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
    if operation == "graph_artifact_verify":
        if not request.artifact_path:
            raise HTTPException(status_code=422, detail={"error": "artifact_path_required"})
        try:
            verified = verify_graph_artifact(request.artifact_path, runtime_dir=settings.runtime_dir)
        except CodeGraphArtifactError as exc:
            raise HTTPException(status_code=422, detail={"error": "graph_artifact_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "artifact_schema_version": CODE_GRAPH_ARTIFACT_SCHEMA_VERSION,
            **verified,
            "execution": {"writes_sqlite_state": False, "writes_runtime_artifact": False, "raw_source_returned": False, "import_apply": False},
        }
    if operation == "graph_artifact_promotion_plan":
        if not request.artifact_path:
            raise HTTPException(status_code=422, detail={"error": "artifact_path_required"})
        try:
            verified = verify_graph_artifact(request.artifact_path, runtime_dir=settings.runtime_dir)
            current_graph = SQLiteCodeGraphStore(database_path).current_snapshot(project, root_id, include_material=False)
            plan = build_graph_artifact_promotion_plan(
                verified,
                project=project,
                root_id=root_id,
                target_snapshot=current_graph,
                detached_signature_b64=request.detached_signature_b64,
                detached_public_key_b64=request.detached_public_key_b64,
                adoption_receipt_digest=request.adoption_receipt_digest,
                rollback_anchor_snapshot_id=request.rollback_anchor_snapshot_id,
                rollback_anchor_digest=request.rollback_anchor_digest,
            )
        except (CodeGraphArtifactError, CodeGraphError) as exc:
            raise HTTPException(status_code=422, detail={"error": "graph_artifact_promotion_plan_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            **plan,
        }
    if operation == "graph_artifact_export":
        if not request.apply:
            return {
                "schema_version": "bhm.public-code-tools.v1",
                "contract_digest": _public_code_contract_digest(),
                "operation": operation,
                "action": "plan",
                "project": project,
                "root_id": root_id,
                "requires_explicit_apply": True,
                "execution": {"writes_sqlite_state": False, "writes_runtime_artifact": False, "raw_source_returned": False},
                "provenance": {"source": "sqlite-authoritative", "authority": "non-authoritative artifact preview"},
            }
        current_graph = SQLiteCodeGraphStore(database_path).current_snapshot(project, root_id, include_material=False)
        if not current_graph:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        try:
            material = SQLiteCodeGraphStore(database_path).snapshot(str(current_graph.get("graph_snapshot_id")), include_material=True, read_only=True)
            previous_id = str(current_graph.get("previous_graph_snapshot_id") or "")
            if previous_id:
                try:
                    previous = SQLiteCodeGraphStore(database_path).snapshot(previous_id, include_material=False, read_only=True)
                except CodeGraphError:
                    previous = {}
                material["previous_graph_snapshot_id"] = previous_id
                material["previous_graph_digest"] = previous.get("graph_digest")
            exported = export_graph_artifact(material, runtime_dir=settings.runtime_dir, project=project, root_id=root_id)
        except (CodeGraphArtifactError, CodeGraphError) as exc:
            raise HTTPException(status_code=422, detail={"error": "graph_artifact_export_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "artifact_schema_version": CODE_GRAPH_ARTIFACT_SCHEMA_VERSION,
            **exported,
            "execution": {"writes_sqlite_state": False, "writes_runtime_artifact": True, "raw_source_returned": False, "import_apply": False},
            "provenance": {"source": "sqlite-authoritative", "authority": "non-authoritative shared artifact", "source_persisted": False},
        }
    if operation == "watch":
        if not request.apply:
            return {
                "schema_version": "bhm.public-code-tools.v1",
                "contract_digest": _public_code_contract_digest(),
                "operation": operation,
                "action": "plan",
                "project": project,
                "root_id": root_id,
                "cycles": request.cycles,
                "interval_seconds": request.interval_seconds,
                "debounce_seconds": request.debounce_seconds,
                "requires_explicit_apply": True,
                "starts_background_daemon": False,
                "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "raw_source_returned": False},
                "provenance": {"source": "local-operator", "authority": "sqlite-authoritative"},
            }
        try:
            watcher = RepositoryWatcher(
                root,
                database_path,
                project=project,
                source=RepositorySourceProvenance(source_url=f"local://{root.name}", owner="operator"),
            )
            watched = watcher.run(cycles=request.cycles, interval_seconds=request.interval_seconds, debounce_seconds=request.debounce_seconds, index_on_change=True)
            graph = None
            if any(event.get("index") for event in watched.get("events") or []):
                graph = build_code_graph(database_path, project=project, root_id=root_id)
        except (RepositoryIndexError, CodeGraphError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"error": "code_watch_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "watch": watched,
            "graph": graph,
            "starts_background_daemon": False,
            "execution": {"writes_sqlite_state": True, "writes_qdrant": False, "raw_source_returned": False, "force_refresh": request.force_refresh},
            "provenance": {"source": "local-operator", "authority": "sqlite-authoritative", "source_persisted": False},
        }
    if operation == "status":
        status = repository_index_status(root, database_path, project=project)
        graph_current = SQLiteCodeGraphStore(database_path).current_snapshot(project, root_id, include_material=False)
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "root": str(root),
            "index": status,
            "graph": graph_current,
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
            "provenance": {"source": "sqlite-authoritative", "root_allowlist": "repos-root"},
        }
    if operation == "index":
        status = repository_index_status(root, database_path, project=project)
        if not request.apply:
            return {
                "schema_version": "bhm.public-code-tools.v1",
                "contract_digest": _public_code_contract_digest(),
                "operation": operation,
                "action": "plan",
                "project": project,
                "root_id": root_id,
                "root": str(root),
                "index": status,
                "requires_explicit_apply": True,
                "force_refresh": request.force_refresh,
                "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
                "provenance": {"source": "local-operator", "license": "operator-owned", "evidence_class": "E0"},
            }
        try:
            indexed = index_repository(
                root,
                database_path,
                project=project,
                source=RepositorySourceProvenance(source_url=f"local://{root.name}", owner="operator"),
                force_refresh=request.force_refresh,
            )
            graph = build_code_graph(database_path, project=project, root_id=root_id) if request.build_graph else None
        except (RepositoryIndexError, CodeGraphError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"error": "code_index_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "action": "index",
            "project": project,
            "root_id": root_id,
            "index": indexed,
            "graph": graph,
            "execution": {"writes_sqlite_state": True, "writes_qdrant": False, "raw_source_returned": False, "force_refresh": request.force_refresh},
            "provenance": {"source": "local-operator", "license": "operator-owned", "evidence_class": "E0"},
        }
    if operation == "trace_evidence":
        observations = _observation_store().load(
            project=project,
            include_archived=False,
            include_purged=False,
            limit=min(int(request.limit), 256),
            newest_first=True,
        )
        try:
            trace_graph = build_trace_graph(
                observations,
                project=project,
                max_events=min(int(request.limit), 256),
                max_nodes=128,
                max_edges=256,
            )
            validation = validate_trace_graph(trace_graph)
        except TraceGraphError as exc:
            raise HTTPException(status_code=422, detail={"error": "trace_graph_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        code_trace_receipt: dict[str, Any] = {
            "schema_version": SERVICE_TRACE_RECEIPT_SCHEMA_VERSION,
            "status": "graph_snapshot_unavailable",
            "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "raw_source_returned": False, "trace_edges_promoted": False},
        }
        current_graph = SQLiteCodeGraphStore(database_path).current_snapshot(project, root_id, include_material=False)
        if current_graph:
            try:
                material = SQLiteCodeGraphStore(database_path).snapshot(str(current_graph.get("graph_snapshot_id")), include_material=True, read_only=True)
                code_trace_receipt = build_service_trace_receipt(
                    material.get("nodes") or [],
                    material.get("edges") or [],
                    graph_snapshot_id=str(current_graph.get("graph_snapshot_id") or ""),
                    graph_digest=str(current_graph.get("graph_digest") or ""),
                    max_hops=4,
                    max_paths=min(int(request.limit), 64),
                )
            except CodeGraphError as exc:
                raise HTTPException(status_code=503, detail={"error": "service_trace_receipt_unavailable", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            **trace_graph,
            "operation": operation,
            "validation": validation,
            "code_trace_receipt": code_trace_receipt,
            "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_retrieval": False, "model_started": False, "raw_source_returned": False, "trace_edges_promoted": False},
        }
    graph_store = SQLiteCodeGraphStore(database_path)
    current = graph_store.current_snapshot(project, root_id, include_material=False)
    if operation == "type_references":
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        current_graph_digest = str(current.get("graph_digest") or "")
        if request.expected_graph_digest and request.expected_graph_digest != current_graph_digest:
            raise HTTPException(status_code=409, detail={"error": "expected_graph_digest_mismatch", "expected_graph_digest": request.expected_graph_digest, "actual_graph_digest": current_graph_digest})
        try:
            material = graph_store.snapshot(str(current.get("graph_snapshot_id")), include_material=True, read_only=True)
            result = build_type_reference_resolution(material.get("nodes") or [], material.get("edges") or [], max_items=request.limit)
        except CodeGraphError as exc:
            raise HTTPException(status_code=503, detail={"error": "type_reference_resolution_unavailable", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": TYPE_REFERENCE_RESOLUTION_SCHEMA_VERSION,
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "graph_snapshot_id": current.get("graph_snapshot_id"),
            "graph_digest": current.get("graph_digest"),
            **result,
            "resolution_quality": build_resolution_quality_receipt(
                type_result=result,
                graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
                graph_digest=current_graph_digest,
                snapshot_digest=str(current.get("snapshot_digest") or ""),
                repository_snapshot_id=str(current.get("repository_snapshot_id") or ""),
                parser_registry_digest=PARSER_REGISTRY_DIGEST,
                language_inventory_digest=LANGUAGE_INVENTORY_DIGEST,
                contract_digest=_public_code_contract_digest(),
                expected_graph_digest=str(request.expected_graph_digest or ""),
            ),
            "provenance": {"source": "sqlite-authoritative-code-graph", "authority": "proposal", "raw_source_returned": False},
        }
    if operation == "bicep_module_resolution":
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        try:
            material = graph_store.snapshot(str(current.get("graph_snapshot_id")), include_material=True, read_only=True)
            result = build_bicep_module_resolution(material.get("nodes") or [], material.get("edges") or [], max_items=request.limit)
        except CodeGraphError as exc:
            raise HTTPException(status_code=503, detail={"error": "bicep_module_resolution_unavailable", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        return {
            "schema_version": BICEP_MODULE_RESOLUTION_SCHEMA_VERSION,
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "graph_snapshot_id": current.get("graph_snapshot_id"),
            "graph_digest": current.get("graph_digest"),
            **result,
            "provenance": {"source": "sqlite-authoritative-code-graph", "authority": "proposal", "raw_source_returned": False},
        }
    if operation in {"code_search", "code_snippet"}:
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "repository_snapshot_unavailable"})
        index_store = SQLiteRepositoryIndexStore(database_path)
        try:
            snapshot = index_store.snapshot(
                str(current.get("repository_snapshot_id") or current.get("snapshot_id") or ""),
                include_files=True,
                read_only=True,
            )
        except (RepositoryIndexError, ValueError) as exc:
            raise HTTPException(status_code=503, detail={"error": "repository_snapshot_unavailable"}) from exc
        files = list(snapshot.get("files") or [])
        try:
            if operation == "code_search":
                semantic_hits: list[dict[str, Any]] = []
                semantic_graph_result: dict[str, Any] | None = None
                baseline_matches: list[dict[str, Any]] = []
                semantic_status = "not_requested"
                semantic_latency_ms: float | None = None
                semantic_readiness: dict[str, Any] | None = None
                embedding_contract = _public_code_embedding_contract()
                if request.semantic_fusion:
                    if not semantic_fusion_enabled():
                        semantic_status = "feature_disabled"
                    else:
                        if _SEMANTIC_READINESS_GATE_ENABLED:
                            try:
                                semantic_readiness = await _semantic_readiness_receipt(
                                    project=project,
                                    current_graph=current,
                                    repository_snapshot=snapshot,
                                    embedding_contract=embedding_contract,
                                )
                            except Exception:
                                semantic_readiness = {
                                    "schema_version": "bhm.semantic-readiness.v1",
                                    "ready": False,
                                    "request_status": "not_ready",
                                    "freshness": "unknown",
                                    "requires_operator_projection": True,
                                    "requires_operator_warmup": True,
                                    "requirements": ["operator_recheck_readiness"],
                                    "failures": ["readiness_probe_failed"],
                                    "execution": {
                                        "provider_called": False,
                                        "model_started": False,
                                        "network_called": False,
                                        "writes_sqlite_state": False,
                                        "writes_qdrant": False,
                                        "raw_source_returned": False,
                                    },
                                }
                            semantic_status = "ready" if bool(semantic_readiness.get("ready")) else "not_ready"
                        else:
                            semantic_status = "enabled"
                        if semantic_status in {"enabled", "ready"}:
                            semantic_started = time.perf_counter()
                            try:
                                raw_hits, _total = await federated_search(
                                    request.query,
                                    project,
                                    limit=min(max(int(request.limit) * 3, 20), 128),
                                    include_archived=False,
                                    include_logs=False,
                                    include_graph_expansion=False,
                                    # CBM code metadata is project-scoped. Do
                                    # not traverse the global BHM memory
                                    # contour on this path; it adds latency
                                    # and can contaminate code-only relevance.
                                    include_global=False,
                                )
                                # Pass projection hits through; code_search extracts
                                # only path/source identifiers and numeric scores.
                                semantic_hits = list(raw_hits or [])
                            except Exception as exc:  # optional channel must not break lexical search
                                semantic_status = "unavailable"
                                semantic_hits = []
                                print(f"[WARN] BHM code semantic fusion unavailable: {exc}", flush=True)
                            finally:
                                semantic_latency_ms = round((time.perf_counter() - semantic_started) * 1000.0, 3)
                if request.search_mode == "metadata":
                    if request.query:
                        baseline_matches = graph_store.search_metadata(str(current.get("graph_snapshot_id") or ""), request.query, limit=request.limit, offset=request.offset)
                    else:
                        baseline_matches = []
                    matches = baseline_matches
                    matches = fuse_code_search_matches(matches, semantic_hits, limit=request.limit, semantic_weight=request.semantic_weight)
                    result = {
                        "schema_version": "bhm.code-search.v1",
                        "query": request.query,
                        "mode": "metadata",
                        "matches": matches,
                        "offset": request.offset,
                        "next_offset": request.offset + len(matches) if len(matches) == request.limit else None,
                        "search_strategy": "sqlite-fts5-metadata" + ("+qdrant-rrf" if semantic_hits and semantic_fusion_enabled() else ""),
                        "scanned_files": 0,
                        "skipped_files": 0,
                        "scanned_bytes": 0,
                        "snapshot_digest": str(snapshot.get("snapshot_digest") or ""),
                        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "source_persisted": False, "raw_source_returned": False, "redacted_snippets_returned": False, "semantic_fusion": bool(semantic_hits and semantic_fusion_enabled())},
                    }
                else:
                    if request.query or not request.semantic_query:
                        baseline_result = search_repository_code(
                            root,
                            files,
                            query=request.query,
                            mode=request.search_mode,
                            limit=request.limit,
                            include_snippets=False,
                            snippet_max_chars=request.snippet_max_chars,
                            snapshot_digest=str(snapshot.get("snapshot_digest") or ""),
                            semantic_hits=None,
                            semantic_weight=request.semantic_weight,
                            offset=request.offset,
                        )
                        baseline_matches = list(baseline_result.get("matches") or [])
                        result = search_repository_code(
                            root,
                            files,
                            query=request.query,
                            mode=request.search_mode,
                            limit=request.limit,
                            include_snippets=request.include_snippets,
                            snippet_max_chars=request.snippet_max_chars,
                            snapshot_digest=str(snapshot.get("snapshot_digest") or ""),
                            semantic_hits=semantic_hits,
                            semantic_weight=request.semantic_weight,
                            offset=request.offset,
                        )
                    else:
                        baseline_matches = []
                        result = {
                            "schema_version": "bhm.code-search.v1",
                            "query": request.query,
                            "mode": request.search_mode,
                            "matches": [],
                            "offset": request.offset,
                            "next_offset": None,
                            "search_strategy": "graph-semantic-only",
                            "scanned_files": 0,
                            "skipped_files": 0,
                            "scanned_bytes": 0,
                            "snapshot_digest": str(snapshot.get("snapshot_digest") or ""),
                        }
                if request.semantic_query is not None:
                    if str(current.get("status") or "completed") != "completed":
                        raise SemanticCodeSearchError("semantic_query requires a completed graph snapshot")
                    try:
                        semantic_material = graph_store.snapshot(
                            str(current.get("graph_snapshot_id") or ""),
                            include_material=True,
                            read_only=True,
                        )
                        semantic_graph_result = semantic_search_metadata(
                            semantic_material.get("nodes") or [],
                            request.semantic_query,
                            limit=request.limit,
                            offset=request.offset,
                            min_score=request.semantic_min_score,
                            max_tokens=request.max_tokens,
                            time_budget_ms=request.time_budget_ms,
                            project=project,
                            root_id=root_id,
                            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
                            graph_digest=str(current.get("graph_digest") or ""),
                            parser_registry_digest=PARSER_REGISTRY_DIGEST,
                        )
                    except (SemanticCodeSearchError, CodeGraphError) as exc:
                        raise HTTPException(status_code=422, detail={"error": "semantic_query_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
                    result.update(
                        {
                            "semantic_query": semantic_graph_result.get("semantic_query"),
                            "semantic_results": semantic_graph_result.get("semantic_results", []),
                            "semantic_result_total": semantic_graph_result.get("total_results", 0),
                            "semantic_result_offset": semantic_graph_result.get("offset", request.offset),
                            "semantic_result_next_offset": semantic_graph_result.get("next_offset"),
                            "semantic_query_receipt": {
                                "schema_version": semantic_graph_result.get("schema_version"),
                                "algorithm": semantic_graph_result.get("algorithm"),
                                "result_digest": semantic_graph_result.get("result_digest"),
                                "scanned_nodes": semantic_graph_result.get("scanned_nodes", 0),
                                "timed_out": semantic_graph_result.get("timed_out", False),
                                "provenance": semantic_graph_result.get("provenance", {}),
                            },
                        }
                    )
                semantic_contract = result.setdefault("semantic_fusion", {})
                semantic_active = bool(semantic_hits and semantic_fusion_enabled())
                if request.semantic_fusion:
                    # The readiness gate already computed the authoritative
                    # runtime SLO from the same graph/outbox epoch.  Reusing
                    # it avoids a second full health/cutover probe (which can
                    # take several seconds on a large SQLite store).
                    runtime_slo_status = str(
                        (semantic_readiness or {}).get("runtime_slo_status")
                        or _fast_semantic_runtime_slo_status()
                    )
                else:
                    runtime_slo_status = "unknown"
                provider_ready = None
                if request.semantic_fusion:
                    provider_ready = bool(_get_provider_warmup_status().get("ready"))
                semantic_observation = build_semantic_observation(
                    snapshot,
                    requested=bool(request.semantic_fusion),
                    request_status=semantic_status,
                    active=semantic_active,
                    feature_enabled=semantic_fusion_enabled(),
                    provider_ready=provider_ready,
                    runtime_slo_status=runtime_slo_status,
                    graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
                    graph_digest=str(current.get("graph_digest") or ""),
                    observed_latency_ms=semantic_latency_ms,
                )
                semantic_contract.update(
                    {
                        "schema_version": semantic_contract.get("schema_version", "bhm.code-search.semantic-fusion.v1"),
                        "requested_hits": len(semantic_hits),
                        "enabled": semantic_fusion_enabled(),
                        "active": semantic_active,
                        "weight": max(0.0, min(float(request.semantic_weight), 0.75)),
                        "source": "qdrant-projection-metadata",
                        "authority": "projection-only",
                        "source_persisted": False,
                        "raw_source_returned": False,
                        "request_status": semantic_status,
                        "embedding_contract": embedding_contract,
                        "readiness_gate": {
                            "enabled": _SEMANTIC_READINESS_GATE_ENABLED,
                            "operator_only": True,
                            "implicit_provider_activation": False,
                        },
                        "observation": semantic_observation,
                        "relevance_receipt": build_semantic_relevance_receipt(
                            baseline_matches,
                            list(result.get("matches") or []),
                            requested=bool(request.semantic_fusion),
                            feature_enabled=semantic_fusion_enabled(),
                            request_status=semantic_status,
                            active=semantic_active,
                            provider_ready=provider_ready,
                            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
                            graph_digest=str(current.get("graph_digest") or ""),
                            snapshot_digest=str(snapshot.get("snapshot_digest") or ""),
                            runtime_slo_status=runtime_slo_status,
                            freshness_receipt=semantic_observation.get("freshness_receipt"),
                            semantic_weight=request.semantic_weight,
                        ),
                        "provenance_receipt": build_semantic_fusion_provenance_receipt(
                            embedding_contract=embedding_contract,
                            baseline_matches=baseline_matches,
                            fused_matches=list(result.get("matches") or []),
                            semantic_hits=len(semantic_hits),
                            requested=bool(request.semantic_fusion),
                            feature_enabled=semantic_fusion_enabled(),
                            active=semantic_active,
                            request_status=semantic_status,
                            snapshot_digest=str(snapshot.get("snapshot_digest") or ""),
                            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
                            graph_digest=str(current.get("graph_digest") or ""),
                            semantic_weight=request.semantic_weight,
                        ),
                    }
                )
                if semantic_readiness is not None:
                    semantic_contract["readiness"] = semantic_readiness
            else:
                if not request.path:
                    raise CodeSearchError("path is required for code_snippet")
                result = get_repository_snippet(
                    root,
                    files,
                    path=request.path,
                    line=request.line,
                    context=request.context,
                    snapshot_digest=str(snapshot.get("snapshot_digest") or ""),
                )
        except (CodeSearchError, CodeGraphError) as exc:
            raise HTTPException(status_code=422, detail={"error": "code_search_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        result.update(
            {
                "operation": operation,
                "contract_digest": _public_code_contract_digest(),
                "project": project,
                "root_id": root_id,
                "root": str(root),
                "provenance": {
                    "source": "indexed-files-live-read",
                    "repository_snapshot_id": snapshot.get("snapshot_id"),
                    "snapshot_digest": snapshot.get("snapshot_digest"),
                    "raw_source_returned": False,
                    "source_persisted": False,
                },
            }
        )
        return result
    if operation == "impact_preview":
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        try:
            material = graph_store.snapshot(str(current.get("graph_snapshot_id")), include_material=True, read_only=True)
            changed_paths = list(request.changed_paths)
            git_context: dict[str, Any] = {"source": "request", "writes_worktree": False, "base_revision": request.base_revision}
            if not changed_paths:
                git_context = collect_git_change_paths(root, base_revision=request.base_revision)
            elif request.base_revision:
                git_context["diff_hunks"] = collect_git_diff_hunks(root, base_revision=request.base_revision, paths=changed_paths)
            conventions = preview_convention_memory(database_path, project=project, root_id=root_id, graph_snapshot_id=str(current.get("graph_snapshot_id")))
            result = build_change_impact_preview(material, changed_paths, conventions=conventions, expected_graph_digest=request.expected_graph_digest)
            result["git_context"] = git_context
            if request.include_git_history and changed_paths:
                result["git_history"] = collect_git_history_stats(root, changed_paths[:8])
            else:
                result["git_history"] = {"commits_considered": 0, "hotspots": [], "cochange": [], "writes_worktree": False, "available": False}
            result["history_correlation"] = build_git_history_correlation_receipt(
                result["git_history"],
                (),
                changed_paths=changed_paths,
            )
            history_symbols = correlate_git_history_to_symbols(result["git_history"], list(material.get("nodes") or []))
            result["commit_symbol_test_history"] = build_commit_symbol_test_history_receipt(
                result["git_history"],
                history_symbols,
                list(material.get("nodes") or []),
                changed_paths=changed_paths,
            )
            diff_hunks = list(git_context.get("diff_hunks") or [])
            hunk_symbols = correlate_diff_hunks_to_symbols(diff_hunks, list(material.get("nodes") or []))
            result["impact_binding"] = build_impact_binding_receipt(
                graph_snapshot_id=material.get("graph_snapshot_id"),
                graph_digest=material.get("graph_digest"),
                expected_graph_digest=request.expected_graph_digest,
                changed_paths=changed_paths,
                diff_hunks=diff_hunks,
                hunk_symbols=hunk_symbols,
                git_history=result["git_history"],
                provenance={"git_metadata_only": True, "graph_metadata_only": True, "raw_source_returned": False},
                execution={"writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "writes_mem0": False, "auto_apply": False, "edge_promotion": False},
            )
            result["risk_receipt"] = build_change_impact_risk_receipt(
                result,
                changed_paths=changed_paths,
                diff_hunks=diff_hunks,
                hunk_symbols=hunk_symbols,
                git_history=result["git_history"],
                impact_binding=result["impact_binding"],
            )
            try:
                head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
            except (OSError, subprocess.CalledProcessError):
                head = ""
            result["git_context"]["head_revision"] = head
            result["operation"] = operation
            result["contract_digest"] = _public_code_contract_digest()
            result["provenance"] = {"source": "sqlite-authoritative+git-metadata", "graph_snapshot_id": current.get("graph_snapshot_id"), "raw_source_returned": False}
            return result
        except (ChangeImpactError, ConventionMemoryError, OSError, subprocess.CalledProcessError) as exc:
            raise HTTPException(status_code=422, detail={"error": "impact_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
    if operation in {"search", "graph", "trace", "impact"}:
        graph_operation = request.graph_operation
        if operation == "search":
            graph_operation = "symbol"
        elif operation == "trace":
            graph_operation = "callers" if graph_operation == "symbol" else graph_operation
        elif operation == "impact":
            graph_operation = "impact"
        try:
            result = query_code_graph(
                database_path,
                project=project,
                root_id=root_id,
                operation=graph_operation,
                query=request.query,
                depth=request.depth,
                limit=request.limit,
                offset=request.offset,
                max_tokens=request.max_tokens,
                time_budget_ms=request.time_budget_ms,
                snapshot_id=request.snapshot_id,
                explain=operation in {"trace", "impact"},
                edge_kinds=request.edge_kinds or None,
                name_pattern=request.name_pattern,
                path_pattern=request.path_pattern,
                label=request.label,
                min_degree=request.min_degree,
                max_degree=request.max_degree,
            )
        except CodeGraphQueryError as exc:
            status_code = 503 if "unavailable" in str(exc).casefold() else 422
            raise HTTPException(status_code=status_code, detail={"error": "code_tool_graph_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
        result["public_operation"] = operation
        result["contract_digest"] = _public_code_contract_digest()
        result["provenance"] = {"source": "sqlite-authoritative", "graph_snapshot_id": result.get("snapshot_id"), "raw_source_returned": False}
        return result
    if operation == "schema":
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "graph_snapshot_id": current.get("graph_snapshot_id"),
            "graph_schema_version": CODE_GRAPH_SCHEMA_VERSION,
            "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION,
            "parser_registry_digest": PARSER_REGISTRY_DIGEST,
            "language_inventory_digest": LANGUAGE_INVENTORY_DIGEST,
            "parser_capabilities": parser_capability_matrix(),
            "summary": current.get("summary") or {},
            "allowed_operations": sorted(CODE_GRAPH_QUERY_OPERATIONS),
            "edge_authority": "sqlite-authoritative",
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False, "arbitrary_sql": False},
        }
    if operation == "coverage":
        index_status = repository_index_status(root, database_path, project=project)
        graph = current or {}
        summary = dict(graph.get("summary") or {})
        parse_status = dict(summary.get("parse_status") or {})
        file_count = int(summary.get("file_count") or 0)
        parsed = int(parse_status.get("parsed") or 0)
        metadata_only = int(parse_status.get("metadata-only") or 0)
        errors = int(parse_status.get("error") or 0)
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "index_fresh": bool(index_status.get("fresh")),
            "graph_snapshot_id": graph.get("graph_snapshot_id"),
            "coverage": {"file_count": file_count, "parsed": parsed, "metadata_only": metadata_only, "errors": errors, "parse_rate": round(parsed / max(file_count, 1), 6), "complete": bool(current and index_status.get("fresh") and errors == 0)},
            "language_inventory_digest": LANGUAGE_INVENTORY_DIGEST,
            "parser_capabilities": parser_capability_matrix(),
            "summary": summary,
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
        }
    if operation == "architecture":
        if current is None:
            raise HTTPException(status_code=503, detail={"error": "graph_snapshot_unavailable"})
        material = graph_store.snapshot(str(current.get("graph_snapshot_id")), include_material=True, read_only=True)
        layer_counts: dict[str, int] = {}
        for node in list(material.get("nodes") or []):
            path = str(node.get("path") or "")
            layer = path.split("/", 1)[0] if path else "<root>"
            layer_counts[layer] = layer_counts.get(layer, 0) + 1
        dependency_counts: dict[str, int] = {}
        outgoing_counts: dict[str, int] = {}
        nodes_by_id = {str(node.get("node_id")): node for node in list(material.get("nodes") or [])}
        for edge in list(material.get("edges") or []):
            edge_kind = str(edge.get("edge_kind"))
            if edge_kind not in {"imports", "calls", "async_calls", "route_handles", "http_calls", "emits", "listens_on", "data_flows", "similar_to", "depends_on", "exposes"}:
                continue
            source = nodes_by_id.get(str(edge.get("source_node_id")))
            target = nodes_by_id.get(str(edge.get("target_node_id")))
            if not source or not target:
                continue
            source_layer = str(source.get("path") or "").split("/", 1)[0] or "<root>"
            target_layer = str(target.get("path") or "").split("/", 1)[0] or "<root>"
            key = f"{source_layer}->{target_layer}"
            dependency_counts[key] = dependency_counts.get(key, 0) + 1
            source_id = str(source.get("node_id") or "")
            outgoing_counts[source_id] = outgoing_counts.get(source_id, 0) + 1
        hotspots = []
        for node_id, edge_count in sorted(outgoing_counts.items(), key=lambda item: (-item[1], item[0]))[:32]:
            node = nodes_by_id.get(node_id) or {}
            hotspots.append({"node_id": node_id, "path": node.get("path") or "", "name": node.get("name") or "", "edge_count": edge_count, "source_ref": (node.get("provenance") or {}).get("source_ref") or ""})
        intelligence = build_architecture_intelligence(
            list(material.get("nodes") or []),
            list(material.get("edges") or []),
            max_items=32,
        )
        explain_receipt = build_architecture_explain_receipt(
            intelligence,
            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
            graph_digest=str(current.get("graph_digest") or ""),
            max_items=32,
        )
        quality_receipt = build_graph_analysis_quality_receipt(
            intelligence,
            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
            graph_digest=str(current.get("graph_digest") or ""),
            node_count=len(material.get("nodes") or []),
            edge_count=len(material.get("edges") or []),
            max_items=32,
        )
        intelligence["quality_receipt"] = quality_receipt
        architecture_memory = build_architecture_memory(
            list(material.get("nodes") or []),
            list(material.get("edges") or []),
            graph_snapshot_id=str(current.get("graph_snapshot_id") or ""),
            graph_digest=str(current.get("graph_digest") or ""),
            repository_snapshot_id=str(current.get("repository_snapshot_id") or ""),
            max_items=32,
        )
        return {
            "schema_version": "bhm.public-code-tools.v1",
            "contract_digest": _public_code_contract_digest(),
            "operation": operation,
            "project": project,
            "root_id": root_id,
            "graph_snapshot_id": current.get("graph_snapshot_id"),
            "architecture": {
                "summary": current.get("summary") or {},
                "layers": [{"name": key, "node_count": value} for key, value in sorted(layer_counts.items())[:64]],
                "dependencies": [{"path": key, "edge_count": value} for key, value in sorted(dependency_counts.items(), key=lambda item: (-item[1], item[0]))[:64]],
                "hotspots": hotspots,
                "intelligence": intelligence,
                "explain_receipt": explain_receipt,
                "quality_receipt": quality_receipt,
                "architecture_memory": architecture_memory,
            },
            "provenance": {"source": "code-graph-summary", "raw_source_returned": False},
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False},
        }
    raise HTTPException(status_code=422, detail={"error": "code_tool_operation_unhandled"})


def _convention_memory_preview_response(request: ConventionMemoryPreviewRequest) -> dict[str, Any]:
    project = _canonical_project(request.project)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    root_id = request.root_id or _code_graph_query_root_id(project, settings.repo_root)
    try:
        return preview_convention_memory(
            database_path,
            project=project,
            root_id=root_id,
            graph_snapshot_id=request.graph_snapshot_id,
        )
    except ConventionMemoryError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        status_code = 503 if "unavailable" in detail.casefold() else 422
        raise HTTPException(status_code=status_code, detail={"error": "convention_preview_rejected", "detail": detail}) from exc


@app.post("/bhm/conventions/preview", include_in_schema=False)
def bhm_conventions_preview(request: ConventionMemoryPreviewRequest) -> dict[str, Any]:
    """Internal read-only convention/architecture-memory preview."""

    return _convention_memory_preview_response(request)


@app.post("/bhm/ui/code-tools", include_in_schema=False)
async def bhm_ui_code_tools(request: PublicCodeToolRequest) -> dict[str, Any]:
    """Read-only UI proxy for the canonical code-tools contract.

    The browser session must not gain the mutating index/watch/artifact-export
    operations exposed to bearer-authenticated operators.
    """

    operation = str(request.operation).strip().casefold()
    allowed = {"schema", "coverage", "code_search", "graph", "architecture", "impact_preview", "status"}
    if operation not in allowed:
        raise HTTPException(status_code=403, detail={"error": "ui_code_tool_operation_rejected"})
    if operation == "code_search" and request.search_mode not in {"metadata", "symbol", "path"}:
        raise HTTPException(status_code=403, detail={"error": "ui_code_search_mode_rejected"})
    safe_request = request.model_copy(update={"include_snippets": False, "semantic_fusion": False, "apply": False})
    return await bhm_public_code_tools(safe_request)


@app.post("/bhm/change-impact/preview", include_in_schema=False)
def bhm_change_impact_preview(request: ChangeImpactPreviewRequest) -> dict[str, Any]:
    """Internal read-only WI-34 impact and edit-preflight preview."""

    project = _canonical_project(request.project)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    root_id = request.root_id or _code_graph_query_root_id(project, settings.repo_root)
    graph_store = SQLiteCodeGraphStore(database_path)
    try:
        current = graph_store.current_snapshot(project, root_id, include_material=False)
        selected_id = request.graph_snapshot_id or (str(current.get("graph_snapshot_id")) if current else "")
        if not selected_id:
            raise ChangeImpactError("current graph snapshot unavailable")
        snapshot = graph_store.snapshot(selected_id, include_material=True, read_only=True)
        if current and selected_id != str(current.get("graph_snapshot_id")):
            snapshot["stale"] = True
        conventions = preview_convention_memory(database_path, project=project, root_id=root_id)
        changed_paths = list(request.changed_paths)
        git_context: dict[str, Any] = {"source": "request", "writes_worktree": False}
        if not changed_paths:
            git_context = collect_git_change_paths(settings.repo_root)
            changed_paths = list(git_context.get("paths") or [])
        result = build_change_impact_preview(snapshot, changed_paths, conventions=conventions, expected_graph_digest=request.expected_graph_digest)
        result["git_context"] = git_context
        if changed_paths:
            try:
                result["git_history"] = collect_git_history_stats(settings.repo_root, changed_paths[:8])
            except (OSError, subprocess.CalledProcessError):
                result["git_history"] = {"commits_considered": 0, "hotspots": [], "cochange": [], "writes_worktree": False, "available": False}
        else:
            result["git_history"] = {"commits_considered": 0, "hotspots": [], "cochange": [], "writes_worktree": False}
        diff_hunks = list(git_context.get("diff_hunks") or [])
        hunk_symbols = correlate_diff_hunks_to_symbols(diff_hunks, list(snapshot.get("nodes") or []))
        history_symbols = correlate_git_history_to_symbols(result["git_history"], list(snapshot.get("nodes") or []))
        result["commit_symbol_test_history"] = build_commit_symbol_test_history_receipt(
            result["git_history"],
            history_symbols,
            list(snapshot.get("nodes") or []),
            changed_paths=changed_paths,
        )
        result["impact_binding"] = build_impact_binding_receipt(
            graph_snapshot_id=snapshot.get("graph_snapshot_id"),
            graph_digest=snapshot.get("graph_digest"),
            expected_graph_digest=request.expected_graph_digest,
            changed_paths=changed_paths,
            diff_hunks=diff_hunks,
            hunk_symbols=hunk_symbols,
            git_history=result["git_history"],
            provenance={"git_metadata_only": True, "graph_metadata_only": True, "raw_source_returned": False},
            execution={"writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "writes_mem0": False, "auto_apply": False, "edge_promotion": False},
        )
        result["risk_receipt"] = build_change_impact_risk_receipt(
            result,
            changed_paths=changed_paths,
            diff_hunks=diff_hunks,
            hunk_symbols=hunk_symbols,
            git_history=result["git_history"],
            impact_binding=result["impact_binding"],
        )
        return result
    except (ChangeImpactError, ConventionMemoryError) as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        status_code = 503 if "unavailable" in detail.casefold() else 422
        raise HTTPException(status_code=status_code, detail={"error": "change_impact_preview_rejected", "detail": detail}) from exc


@app.post("/bhm/context/unified/compile", include_in_schema=False)
async def bhm_unified_context_compile(request: UnifiedContextCompileRequest) -> dict[str, Any]:
    """Compile memory/code/convention/task/doc/ops channels without MCP schema drift."""

    await _ensure_provider_warmup_ready()
    project_name = _canonical_project(request.project)
    candidate_limit = min(max(int(request.limit) * 3, 20), 50)
    hits, total = await federated_search(request.query, project_name, limit=candidate_limit, include_archived=False, include_logs=False)
    strict_hits = _strict_retrieval_hits(hits, project_name=project_name, include_archived=False, include_logs=False, limit=candidate_limit)
    buckets: dict[str, list[dict[str, Any]]] = {"memory": [], "tasks": [], "docs": [], "ops": []}
    for hit in strict_hits:
        item = _context_item_from_vector_hit(hit)
        bucket = classify_context_item(item)
        buckets.setdefault(bucket, buckets["memory"]).append(item)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    root_id = _code_graph_query_root_id(project_name, settings.repo_root)
    try:
        result = build_unified_context_from_graph(
            database_path,
            project=project_name,
            root_id=root_id,
            query=request.query,
            memory_items=buckets["memory"],
            task_items=buckets["tasks"],
            doc_items=buckets["docs"],
            ops_items=buckets["ops"],
            code_operation=request.code_operation,
            include_code=request.include_code,
            include_conventions=request.include_conventions,
            include_proposals=request.include_proposals,
            token_budget=request.token_budget,
            limit=request.limit,
            time_budget_ms=request.time_budget_ms,
        )
    except UnifiedContextError as exc:
        raise HTTPException(status_code=422, detail={"error": "unified_context_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
    result["retrieval"] = {**(result.get("retrieval") or {}), "total": total, "candidate_count": len(hits), "eligible_count": len(strict_hits)}
    return result


@app.post("/bhm/session-capture/preview", include_in_schema=False)
def bhm_session_capture_preview(request: SessionCapturePreviewRequest) -> dict[str, Any]:
    """Internal read-only session capture and progressive disclosure preview."""

    project_name = _canonical_project(request.project)
    observations = _observation_store().load(
        project=project_name,
        include_archived=False,
        include_purged=False,
        limit=256,
        newest_first=True,
    )
    sessions = _load_session_records()
    memories = [item for item in _load_live_memories() if item.get("project") == project_name]
    try:
        return build_session_capture_preview(
            observations,
            session_records=sessions,
            memories=memories,
            project=project_name,
            session_id=request.session_id or "",
            disclosure=request.disclosure,
            token_budget=request.token_budget,
            max_items=request.max_items,
            stale_days=request.stale_days,
            undo_window_seconds=request.undo_window_seconds,
        )
    except SessionCaptureError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "session_capture_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc


def _memory_graph_database_path() -> Path:
    return resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path


@app.post("/bhm/memory-graph/query", include_in_schema=False)
def bhm_memory_graph_query(request: MemoryGraphQueryRequest) -> dict[str, Any]:
    """Internal bounded temporal memory-graph query."""

    try:
        return query_memory_graph(
            _memory_graph_database_path(),
            project=_canonical_project(request.project),
            operation=request.operation,
            query=request.query,
            snapshot_id=request.snapshot_id,
            as_of=request.as_of,
            depth=request.depth,
            limit=request.limit,
            max_tokens=request.max_tokens,
            time_budget_ms=request.time_budget_ms,
        )
    except MemoryGraphError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        raise HTTPException(status_code=503 if "unavailable" in detail.casefold() else 422, detail={"error": "memory_graph_query_rejected", "detail": detail}) from exc


@app.post("/bhm/memory-graph/explain", include_in_schema=False)
def bhm_memory_graph_explain(request: MemoryGraphQueryRequest) -> dict[str, Any]:
    """Internal temporal graph query with deterministic reason codes."""

    try:
        return explain_memory_graph(
            _memory_graph_database_path(),
            project=_canonical_project(request.project),
            operation=request.operation,
            query=request.query,
            snapshot_id=request.snapshot_id,
            as_of=request.as_of,
            depth=request.depth,
            limit=request.limit,
            max_tokens=request.max_tokens,
            time_budget_ms=request.time_budget_ms,
        )
    except MemoryGraphError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        raise HTTPException(status_code=503 if "unavailable" in detail.casefold() else 422, detail={"error": "memory_graph_explain_rejected", "detail": detail}) from exc


@app.post("/bhm/task-graph/query", include_in_schema=False)
def bhm_task_graph_query(request: TaskGraphQueryRequest) -> dict[str, Any]:
    """Internal bounded task dependency/governance query."""

    try:
        return query_task_graph(
            _memory_graph_database_path(),
            project=_canonical_project(request.project),
            operation=request.operation,
            query=request.query,
            snapshot_id=request.snapshot_id,
            limit=request.limit,
            max_tokens=request.max_tokens,
            time_budget_ms=request.time_budget_ms,
        )
    except TaskGraphError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        raise HTTPException(status_code=503 if "unavailable" in detail.casefold() else 422, detail={"error": "task_graph_query_rejected", "detail": detail}) from exc


@app.post("/bhm/task-graph/explain", include_in_schema=False)
def bhm_task_graph_explain(request: TaskGraphQueryRequest) -> dict[str, Any]:
    """Internal task governance query with deterministic reason codes."""

    try:
        return explain_task_graph(
            _memory_graph_database_path(),
            project=_canonical_project(request.project),
            operation=request.operation,
            query=request.query,
            snapshot_id=request.snapshot_id,
            limit=request.limit,
            max_tokens=request.max_tokens,
            time_budget_ms=request.time_budget_ms,
        )
    except TaskGraphError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        raise HTTPException(status_code=503 if "unavailable" in detail.casefold() else 422, detail={"error": "task_graph_explain_rejected", "detail": detail}) from exc


@app.post("/bhm/llm/code-fabric/plan", include_in_schema=False)
def bhm_llm_code_fabric_plan(request: LLMCodeFabricPlanRequest) -> dict[str, Any]:
    """Internal proposal-only code-intelligence LLM plan."""

    try:
        return build_code_fabric_plan(
            request.task_type,
            request.payload,
            project=_canonical_project(request.project),
            context_digest=request.context_digest,
            required_capabilities=request.required_capabilities,
            context_tokens=request.context_tokens,
            sensitivity=request.sensitivity,
            mutation_requested=request.mutation_requested,
            confidence=request.confidence,
            evidence_count=request.evidence_count,
            risk_flags=request.risk_flags,
            operator_approved=request.operator_approved,
        )
    except LLMCodeFabricError as exc:
        raise HTTPException(status_code=422, detail={"error": "llm_code_fabric_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


@app.post("/bhm/factories/preview", include_in_schema=False)
def bhm_factories_preview(request: FactoryIntegrationPreviewRequest) -> dict[str, Any]:
    """Internal evidence-first QA/incident/documentation crosswalk preview."""

    try:
        return build_factory_integration_preview(
            request.artifacts,
            request.documents,
            project=_canonical_project(request.project),
            changed_paths=request.changed_paths,
            code_items=request.code_items,
            task_items=request.task_items,
            risk_class=request.risk_class,
            max_items=request.max_items,
        )
    except FactoryIntegrationError as exc:
        raise HTTPException(status_code=422, detail={"error": "factory_integration_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


@app.post("/bhm/mcp/unified-contract/preview", include_in_schema=False)
def bhm_unified_mcp_contract_preview(request: UnifiedMcpContractPreviewRequest) -> dict[str, Any]:
    """Internal read-only MCP/hooks/client contract preview."""

    try:
        return build_unified_mcp_contract(
            manifest_path=request.manifest_path,
            initialize_response=request.initialize_response,
            catalog_response=request.catalog_response,
            client_snapshots=request.client_snapshots,
            native_mcp=request.native_mcp,
            hook_profile=request.hook_profile,
        )
    except UnifiedMcpContractError as exc:
        raise HTTPException(status_code=422, detail={"error": "unified_mcp_contract_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


@app.post("/bhm/capability-router/preview", include_in_schema=False)
def bhm_capability_router_preview(request: CapabilityRoutePreviewRequest) -> dict[str, Any]:
    """Internal proposal-only multi-agent capability route preview."""

    try:
        return build_capability_route_plan(
            request.task_type,
            project=_canonical_project(request.project),
            scope=request.scope,
            required_capabilities=request.required_capabilities,
            context_tokens=request.context_tokens,
            confidence=request.confidence,
            sensitivity=request.sensitivity,
            mutation_requested=request.mutation_requested,
            evidence_count=request.evidence_count,
            risk_flags=request.risk_flags,
            operator_approved=request.operator_approved,
            local_capabilities=request.local_capabilities,
            measurements=request.measurements,
            models=request.models,
            claim_state=request.claim_state,
        )
    except CapabilityRouterError as exc:
        raise HTTPException(status_code=422, detail={"error": "capability_route_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


@app.post("/bhm/human-ui/preview", include_in_schema=False)
def bhm_human_ui_preview(request: HumanUiBridgePreviewRequest) -> dict[str, Any]:
    """Internal bounded human-surface and optional Obsidian preview."""

    try:
        return build_human_ui_bridge_preview(
            project=_canonical_project(request.project) if request.project else None,
            nodes=request.nodes,
            links=request.links,
            selected_id=request.selected_id,
            provenance=request.provenance,
            review_items=request.review_items,
            task_items=request.task_items,
            context_packet=request.context_packet,
            mcp_state=request.mcp_state,
            obsidian_export=request.obsidian_export,
            obsidian_import=request.obsidian_import,
            snapshot_id=request.snapshot_id,
            generated_at=request.generated_at,
        )
    except HumanUiBridgeError as exc:
        raise HTTPException(status_code=422, detail={"error": "human_ui_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


@app.post("/bhm/migration/preview", include_in_schema=False)
def bhm_migration_preview(request: MigrationPreviewRequest) -> dict[str, Any]:
    """Internal dry-run migration and compatibility preview."""

    try:
        kwargs: dict[str, Any] = {
            "source_kind": request.source_kind,
            "source_url": request.source_url,
            "source_commit": request.source_commit,
            "source_license": request.source_license,
            "input_schema": request.input_schema,
            "reviewer": request.reviewer,
            "project": _canonical_project(request.project) if request.project else None,
            "dry_run": request.dry_run,
        }
        if request.approved_licenses:
            kwargs["approved_licenses"] = request.approved_licenses
        return build_migration_preview(request.records, **kwargs)
    except MigrationCompatibilityError as exc:
        raise HTTPException(status_code=422, detail={"error": "migration_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


@app.post("/bhm/security/trust-boundary/preview", include_in_schema=False)
def bhm_security_trust_boundary_preview(request: SecurityTrustBoundaryPreviewRequest) -> dict[str, Any]:
    """Internal read-only security, prompt-injection and trust preview."""

    try:
        return build_security_trust_boundary_preview(
            request.items,
            project=_canonical_project(request.project),
            source_kind=request.source_kind,
            source_url=request.source_url,
            source_commit=request.source_commit,
            source_license=request.source_license,
            reviewer=request.reviewer,
            project_roots=request.project_roots,
            paths=request.paths,
            mcp_endpoints=request.mcp_endpoints,
            proposed_actions=request.proposed_actions,
            route=request.route,
            method=request.method,
            capability=request.capability,
            mutation_requested=request.mutation_requested,
            operator_approved=request.operator_approved,
            feature_enabled=request.feature_enabled,
            max_items=request.max_items,
        )
    except SecurityTrustBoundaryError as exc:
        raise HTTPException(status_code=422, detail={"error": "security_trust_boundary_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


@app.post("/bhm/llm/qa-incident/preview")
def bhm_llm_qa_incident_preview(request: QAIncidentPreviewRequest) -> dict[str, Any]:
    """Build QA/incident proposals and deterministic evidence without execution."""

    try:
        preview = build_qa_incident_preview(
            request.artifacts,
            project=request.project,
            changed_paths=request.changed_paths,
            release_candidate=request.release_candidate,
            feature_flags=request.feature_flags,
            max_items=request.max_items,
        )
    except QAIncidentFactoryError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "qa_incident_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


@app.post("/bhm/llm/documentation-factory/preview")
def bhm_llm_documentation_factory_preview(request: DocumentationFactoryPreviewRequest) -> dict[str, Any]:
    """Build documentation/ops/vision patches without writing or OCR execution."""

    try:
        preview = build_documentation_factory_preview(
            request.documents,
            project=request.project,
            locale=request.locale,
            vision_confirmed=request.vision_confirmed,
            vision_assets=request.vision_assets,
            feature_flags=request.feature_flags,
            max_patches=request.max_patches,
        )
    except DocumentationFactoryError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "documentation_factory_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


@app.post("/bhm/llm/night-shift/preview")
def bhm_llm_night_shift_preview(request: NightShiftPreviewRequest) -> dict[str, Any]:
    """Plan safe queued jobs with resource pauses; never start the worker."""

    try:
        preview = build_night_shift_preview(
            request.jobs,
            resource_snapshot=request.resource_snapshot,
            maintenance_window_open=request.maintenance_window_open,
            user_active=request.user_active,
            dry_run=request.dry_run,
            max_jobs=request.max_jobs,
        )
    except NightShiftError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "night_shift_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return preview


@app.post("/bhm/llm/model-router/decide")
def bhm_llm_model_router_decide(request: ModelRouterDecisionRequest) -> dict[str, Any]:
    """Return a local capability/profile route or a fail-closed rejection."""

    try:
        decision = route_model(
            request.task_type,
            required_capabilities=request.required_capabilities,
            context_tokens=request.context_tokens,
            measurements=request.measurements,
            models=request.models,
        )
    except ModelRouterError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "model_router_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    return decision.as_dict()


@app.get("/bhm/llm/model-router")
def bhm_llm_model_router_snapshot() -> dict[str, Any]:
    """Expose local router capabilities and measured-profile status."""

    return router_snapshot()


@app.post("/bhm/llm/cache/preview")
def bhm_llm_cache_preview(request: LLMCachePreviewRequest) -> dict[str, Any]:
    """Build a cache/prefix/invalidation proposal without persisting input or result."""

    try:
        identity = build_cache_identity(
            request.content,
            request.prompt,
            project=request.project,
            prompt_version=request.prompt_version,
            model_digest=request.model_digest,
            parameters=request.parameters,
            prompt_prefix=request.prompt_prefix,
        )
        preview = build_cache_preview(
            identity,
            result=request.result,
            result_supplied=request.result_supplied,
        )
    except LLMCacheError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "llm_cache_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc
    lookup: dict[str, Any] = {
        "inspected": bool(request.inspect_store),
        "exact_hit": False,
        "prefix_candidates": [],
    }
    if request.inspect_store and identity.cacheable:
        lookup["exact_hit"] = _LLM_CACHE_STORE.get(identity, touch=False) is not None
        lookup["prefix_candidates"] = _LLM_CACHE_STORE.find_prefix(identity, limit=request.prefix_limit)
    preview["lookup"] = lookup
    preview["store"] = _LLM_CACHE_STORE.status()
    return preview


@app.get("/bhm/llm/cache")
def bhm_llm_cache_status() -> dict[str, Any]:
    """Expose bounded cache counters and policy without returning payloads."""

    return {
        "schema_version": LLM_CACHE_POLICY_VERSION,
        "store": _LLM_CACHE_STORE.status(),
        "prefix_reuse": True,
        "invalidation": True,
        "execution_enabled": False,
        "writes_performed": False,
        "auto_apply": False,
    }


@app.post("/bhm/llm/jobs")
def bhm_llm_submit_job(request: LLMJobSubmitRequest) -> dict[str, Any]:
    """Persist a sanitized delegation request; a worker is intentionally not started here."""

    try:
        safe = sanitize_llm_value(
            request.payload,
            source="llm-delegation-ingress",
            project=request.project,
        )
    except LLMSafetyViolation as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "llm_ingress_rejected",
                "policy_version": LLM_SAFETY_POLICY_VERSION,
                "detail": redact_secret_text(str(exc)).value[:500],
            },
        ) from exc

    injection_findings = list(
        scan_prompt_injection(json.dumps(safe.value, ensure_ascii=False, default=str))
    )
    safety_provenance = dict(safe.provenance)
    safety_provenance["injection_findings"] = injection_findings
    stored_payload = {
        "contract_version": "bhm.llm.delegation.v1",
        "input": safe.value,
        "safety": safety_provenance,
    }
    try:
        job_id = deterministic_llm_job_id(request.idempotency_key)
        governor = _llm_governor()
        decision = governor.admit(
            AdmissionRequest(
                job_id=job_id,
                workload=request.workload,
                max_wall_seconds=request.max_wall_seconds,
                max_output_tokens=request.max_output_tokens,
            )
        )
    except LLMResourceGovernorError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_governor_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
        ) from exc

    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": "llm_admission_denied", "decision": decision.as_dict()},
            headers={"Retry-After": "5"},
        )

    try:
        try:
            enqueue_result = _LLM_JOB_QUEUE.enqueue(
                idempotency_key=request.idempotency_key,
                job_type=request.job_type,
                payload=stored_payload,
                project=request.project,
                priority=request.priority,
                max_attempts=request.max_attempts,
            )
        except LLMJobQueueFull as exc:
            raise HTTPException(
                status_code=429,
                detail={"error": "llm_job_queue_full", "pending": exc.pending, "capacity": exc.capacity},
                headers={"Retry-After": "5"},
            ) from exc
        except LLMJobIdempotencyCollision as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "llm_job_idempotency_collision", "idempotency_key": exc.idempotency_key},
            ) from exc
        except LLMJobQueueError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
                headers={"Retry-After": "5"},
            ) from exc
    finally:
        # P17.7 exposes durable delegation only; no worker reservation is held.
        governor.release(job_id)

    job = _LLM_JOB_QUEUE.get(enqueue_result.job_id)
    if job is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_disappeared", "job_id": enqueue_result.job_id},
        )
    return {
        "accepted": True,
        "inserted": enqueue_result.inserted,
        "execution_enabled": False,
        "job": _llm_public_job(job),
        "admission": {
            "decision": decision.as_dict(),
            "reservation_held": False,
        },
        "safety": {
            "policy_version": LLM_SAFETY_POLICY_VERSION,
            "redaction_count": safe.provenance.get("redaction_count", 0),
            "injection_findings": injection_findings,
            "authority": PROPOSAL_AUTHORITY,
            "auto_apply": False,
            "requires_approval": True,
        },
    }


@app.get("/bhm/llm/jobs/{job_id}")
def bhm_llm_job_status(job_id: str) -> dict[str, Any]:
    try:
        job = _LLM_JOB_QUEUE.get(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    return {"job": _llm_public_job(job), "execution_enabled": False}


@app.get("/bhm/llm/jobs/{job_id}/result")
def bhm_llm_job_result(job_id: str) -> Any:
    try:
        job = _LLM_JOB_QUEUE.get(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    status = str(job.get("status") or "")
    if status in {"queued", "processing"}:
        return JSONResponse(
            status_code=202,
            content={
                "job_id": job_id,
                "status": status,
                "result_available": False,
                "execution_enabled": False,
            },
        )
    if status != "completed":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "llm_job_result_unavailable",
                "job_id": job_id,
                "status": status,
                "last_error": redact_secret_text(str(job.get("last_error") or "")).value[:500] or None,
            },
        )
    try:
        result = job.get("result")
        proposal = (
            result
            if isinstance(result, dict) and result.get("authority") == PROPOSAL_AUTHORITY
            else build_proposal_envelope(
                job_id=job_id,
                output=result,
                provenance={
                    "project": job.get("project", "blackholememory"),
                    "source": "llm-delegation-result",
                    "queue_schema": LLM_JOB_QUEUE_SCHEMA_VERSION,
                },
            )
        )
    except LLMSafetyViolation as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "llm_result_safety_validation_failed",
                "policy_version": LLM_SAFETY_POLICY_VERSION,
                "detail": redact_secret_text(str(exc)).value[:500],
            },
        ) from exc
    return {
        "job_id": job_id,
        "status": "completed",
        "result": proposal,
        "authority": PROPOSAL_AUTHORITY,
        "auto_apply": False,
        "requires_approval": True,
        "execution_enabled": False,
    }


@app.post("/bhm/llm/jobs/{job_id}/cancel")
def bhm_llm_cancel_job(job_id: str) -> dict[str, Any]:
    try:
        job = _LLM_JOB_QUEUE.cancel(job_id)
    except LLMJobQueueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_job_queue_unavailable", "detail": redact_secret_text(str(exc)).value[:500]},
            headers={"Retry-After": "5"},
        ) from exc
    _llm_governor().release(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "llm_job_not_found", "job_id": job_id})
    return {"cancelled": str(job.get("status")) == "cancelled", "execution_enabled": False, "job": _llm_public_job(job)}


@app.get("/bhm/telemetry/usage")
def bhm_usage_telemetry() -> dict[str, Any]:
    """Return bounded REST/MCP usage aggregates without request content."""

    return _USAGE_TELEMETRY.snapshot()


@app.get("/bhm/telemetry/retrieval")
def bhm_retrieval_telemetry() -> dict[str, Any]:
    """Return bounded retrieval usefulness funnel aggregates."""

    return _RETRIEVAL_FUNNEL.snapshot()


@app.get("/bhm/telemetry/llm")
def bhm_llm_telemetry() -> dict[str, Any]:
    """Return bounded local-LLM queue/gateway/GPU aggregates without raw content."""

    return get_llm_telemetry().snapshot()


@app.get("/bhm/telemetry/feedback-tuning")
def bhm_feedback_tuning(project: str = "") -> dict[str, Any]:
    """Return review-only tuning suggestions from explicit feedback streams."""

    project_name = _canonical_project(project or settings.qdrant_collection)
    usefulness = summarize_explicit_usefulness(_RETRIEVAL_FUNNEL.snapshot(), project=project_name)
    quality = summarize_quality_feedback(_load_live_memories(), project=project_name)
    profile_budgets = {
        name: profile.token_budget
        for name, profile in load_context_profiles(settings.repo_root)[1].items()
    }
    return build_feedback_tuning(
        usefulness=usefulness,
        quality=quality,
        profile_budgets=profile_budgets,
    )


@app.get("/bhm/mcp/http/status", include_in_schema=False)
def bhm_mcp_http_status() -> dict[str, Any]:
    """Return truthful SDK-owned Streamable HTTP session state."""

    return _MCP_STREAMABLE_HTTP.contract_snapshot()


@app.get("/bhm/telemetry/mcp-panel")
def bhm_mcp_panel() -> dict[str, Any]:
    """Return the read-only configured/connected/catalog/runtime MCP panel contract."""

    user_root_value = os.environ.get("USERPROFILE", "").strip()
    configured = load_configured_sources(
        settings.repo_root,
        user_root=Path(user_root_value) if user_root_value else None,
    )
    attach = {}
    http_sessions = _MCP_STREAMABLE_HTTP.sessions.snapshot()
    connection = {"status": "streamable_http", "connections": []}
    telemetry = {"schema_version": "bhm.mcp.streamable-http.telemetry.v1", "recent_events": []}
    runtime = {
        "ready": health_ready(),
        "cutover": health_cutover(),
        "slo": bhm_health_slo(),
    }
    return build_mcp_panel_snapshot(
        configured=configured,
        attach=attach,
        http_sessions=http_sessions,
        connection=connection,
        telemetry=telemetry,
        runtime=runtime,
    )


def _canonical_runtime_repo_root() -> Path:
    """Return the package-owned repository root for internal repair tooling."""

    return Path(__file__).resolve().parents[2]


@app.get("/bhm/mcp/repair/preview")
def bhm_mcp_repair_preview() -> dict[str, Any]:
    """Build a read-only repair plan scoped to BHM registrations only."""

    try:
        return build_repair_preview(repo_root=_canonical_runtime_repo_root(), panel=bhm_mcp_panel())
    except McpRepairError as exc:
        raise HTTPException(status_code=422, detail={"code": "mcp_repair_preview_failed", "reason": str(exc)}) from exc


@app.get("/bhm/mcp/repair/reprobe")
def bhm_mcp_repair_reprobe() -> dict[str, Any]:
    """Re-read BHM panel and adapter presence without changing live state."""

    try:
        return build_reprobe(repo_root=_canonical_runtime_repo_root(), panel=bhm_mcp_panel())
    except McpRepairError as exc:
        raise HTTPException(status_code=422, detail={"code": "mcp_repair_reprobe_failed", "reason": str(exc)}) from exc


@app.post("/bhm/mcp/repair/reconnect")
def bhm_mcp_repair_reconnect(request: McpRepairRequest) -> dict[str, Any]:
    """Attempt only the BHM-scoped repair boundary, then re-probe it."""

    try:
        return execute_reconnect(
            repo_root=_canonical_runtime_repo_root(),
            panel_before=bhm_mcp_panel(),
            panel_after=bhm_mcp_panel,
            clients=request.clients,
            repair_id=request.repair_id,
            confirm=request.confirm,
            apply_adapters=request.apply_adapters,
        )
    except McpRepairError as exc:
        raise HTTPException(status_code=422, detail={"code": "mcp_repair_reconnect_failed", "reason": str(exc)}) from exc


@app.post("/bhm/mcp/repair/rollback")
def bhm_mcp_repair_rollback(request: McpRepairRequest) -> dict[str, Any]:
    """Restore only the exact BHM adapter backup referenced by a repair plan."""

    if not request.repair_id:
        raise HTTPException(status_code=422, detail={"code": "repair_id_required"})
    try:
        return execute_rollback(
            repo_root=_canonical_runtime_repo_root(),
            repair_id=request.repair_id,
            panel_after=bhm_mcp_panel,
            confirm=request.confirm,
        )
    except McpRepairError as exc:
        raise HTTPException(status_code=422, detail={"code": "mcp_repair_rollback_failed", "reason": str(exc)}) from exc


@app.get("/bhm/telemetry/surface-report")
async def bhm_surface_report() -> dict[str, Any]:
    from . import bhm_mcp

    tools = await bhm_mcp.mcp.list_tools()
    return build_surface_report(
        mcp_tools=tools,
        public_openapi=build_openapi_schema(app, "public"),
        admin_openapi=build_openapi_schema(app, "admin"),
        usage_snapshot=_USAGE_TELEMETRY.snapshot(),
    )


@app.get("/bhm/telemetry/qdrant-catalog")
def bhm_qdrant_catalog() -> dict[str, Any]:
    """Return a read-only lifecycle catalog for every live Qdrant collection."""

    return build_qdrant_catalog(
        get_qdrant_client(),
        backup_root=settings.runtime_dir / "live-memory" / "qdrant-quarantine-backups",
        qdrant_url=settings.qdrant_url,
    )


@app.get("/health/cutover")
def health_cutover() -> dict:
    report = dependency_report()
    storage = storage_runtime_state()
    memory_store = _memory_store_state()
    fallback_active = _fallback_grace_active()
    return health_cutover_payload(
        dependency_report=report,
        storage=storage.as_dict(),
        memory_store=memory_store.as_dict(),
        fallback_mode=_configured_fallback_mode(),
        fallback_active=fallback_active,
        mem0_plan=mem0_runtime_plan(),
    )


@app.get("/bhm/health/slo")
def bhm_health_slo(
    max_hook_queue_pending: int = 100,
    max_hook_queue_failed: int = 0,
    max_hook_queue_oldest_age_ms: int = 30_000,
    max_projection_pending: int = 0,
    max_projection_failed: int = 0,
    require_provider_ready: bool = True,
) -> dict:
    """Expose a read-only, bounded health/SLO contract for operators and agents."""

    budgets = {
        "hook_queue_pending": max(int(max_hook_queue_pending), 0),
        "hook_queue_failed": max(int(max_hook_queue_failed), 0),
        "hook_queue_oldest_age_ms": max(int(max_hook_queue_oldest_age_ms), 0),
        "projection_pending": max(int(max_projection_pending), 0),
        "projection_failed": max(int(max_projection_failed), 0),
        "require_provider_ready": bool(require_provider_ready),
    }
    ready = health_ready()
    cutover = health_cutover()
    warmup = _get_provider_warmup_status()
    if _hook_queue_path().exists():
        queue_status = _hook_queue().status()
    else:
        queue_status = {
            "pending": 0,
            "counts": {"queued": 0, "processing": 0, "failed": 0},
            "oldestQueuedAgeMs": 0,
        }
    memory_store = _memory_store_state()
    outbox = {
        "available": False,
        "pending": 0,
        "processing": 0,
        "failed": 0,
        "dead_letter": 0,
        "completed": 0,
        "total": 0,
    }
    if memory_store.configured_mode == MemoryStoreMode.SQLITE_AUTHORITATIVE.value:
        try:
            outbox = {"available": True, **_memory_service().outbox_status()}
        except MemoryServiceNotReady:
            outbox["error"] = "memory_service_unavailable"

    return health_slo_payload(
        budgets=budgets,
        ready=ready,
        cutover=cutover,
        provider_warmup=warmup,
        queue_status=queue_status,
        outbox=outbox,
        service=settings.app_name,
        generated_at=_utc_now_iso(),
    )


@app.get("/graph/status")
def graph_status() -> dict:
    state = graph.invoke({"action": "status"})
    return {"ok": True, "state": state}


@app.get("/bhm/graph")
def bhm_graph(
    project: str | None = None,
    limit: int = 220,
    tag_limit: int = 24,
    include_tags: bool = True,
    include_observations: bool = True,
) -> dict:
    payload = build_galaxy_graph(
        GalaxyOptions(
            project=project,
            limit=max(40, min(limit, 500)),
            tag_limit=max(0, min(tag_limit, 60)),
            include_tags=include_tags,
            include_observations=include_observations,
        ),
        memory_records=_load_live_memories(),
    )
    payload["camera"] = {"distance": camera_distance_for(payload["summary"]["node_count"])}
    return {"ok": True, **payload}


@app.get("/bhm/galaxy/data", response_model=GalaxyDataResponse)
async def bhm_galaxy_data(
    project: str | None = None,
    limit: int = 1000,
    domain: str = "all",
) -> dict:
    """Return the global Galaxy view, optionally narrowed to BHM memory or CBM code."""
    return await _build_galaxy_data(project, max(0, min(limit, 5000)), domain=domain)


@app.post("/bhm/ui/session/mint")
def bhm_ui_session_mint(request: Request) -> JSONResponse:
    principal = getattr(request.state, "bhm_caller_principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    bootstrap_token = _UI_SESSIONS.mint_bootstrap(principal)
    return JSONResponse(
        content={
            "ok": True,
            "schema_version": "bhm.ui.bootstrap.v1",
            "bootstrap_token": bootstrap_token,
            "expires_in_seconds": int(BOOTSTRAP_TTL_SECONDS),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/bhm/ui/session/bootstrap")
def bhm_ui_session_bootstrap(request: Request) -> JSONResponse:
    """Bootstrap a local browser UI without requiring the desktop launcher.

    This route is anonymous only at the HTTP auth layer; it is still restricted
    to same-origin loopback browser requests and exchanges a one-time token for
    the normal HttpOnly UI session cookie. Remote callers remain fail-closed.
    """

    if not _ui_request_is_loopback(request) or not _ui_browser_request_is_same_origin(request):
        return JSONResponse(
            status_code=403,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": "ui_direct_bootstrap_loopback_only"}},
        )
    existing = _UI_SESSIONS.resolve_session(request.cookies.get(UI_SESSION_COOKIE))
    if existing is not None:
        return JSONResponse(
            content={
                "ok": True,
                "schema_version": "bhm.ui.bootstrap.v1",
                "session": True,
                "expires_in_seconds": int(SESSION_TTL_SECONDS),
            },
            headers={"Cache-Control": "no-store"},
        )
    principal = configured_caller_principal()
    if principal is None:
        return JSONResponse(
            status_code=503,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": "caller_auth_not_configured"}},
        )
    bootstrap_token = _UI_SESSIONS.mint_bootstrap(principal)
    return JSONResponse(
        content={
            "ok": True,
            "schema_version": "bhm.ui.bootstrap.v1",
            "session": False,
            "bootstrap_token": bootstrap_token,
            "expires_in_seconds": int(BOOTSTRAP_TTL_SECONDS),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/bhm/ui/session/exchange")
def bhm_ui_session_exchange(request: Request, payload: UiSessionExchangeRequest) -> JSONResponse:
    if not _ui_browser_request_is_same_origin(request, require_origin=True):
        return JSONResponse(
            status_code=403,
            content={"detail": {"code": "ui_session_origin_rejected"}},
        )
    exchanged = _UI_SESSIONS.exchange_bootstrap(payload.bootstrap_token)
    if exchanged is None:
        return JSONResponse(
            status_code=401,
            headers={"Cache-Control": "no-store"},
            content={"detail": {"code": "ui_bootstrap_invalid_or_expired"}},
        )
    session_token, _principal = exchanged
    response = JSONResponse(
        content={
            "ok": True,
            "schema_version": "bhm.ui.session.v1",
            "expires_in_seconds": int(SESSION_TTL_SECONDS),
        },
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        key=UI_SESSION_COOKIE,
        value=session_token,
        max_age=int(SESSION_TTL_SECONDS),
        path="/bhm",
        secure=request.url.scheme.casefold() == "https",
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/bhm/ui/session/status")
def bhm_ui_session_status(request: Request) -> dict:
    principal = getattr(request.state, "bhm_caller_principal", None)
    return {
        "ok": True,
        **_UI_SESSIONS.snapshot(),
        "authenticated": principal is not None,
        "auth_kind": str(getattr(request.state, "bhm_auth_kind", "unknown")),
        "caller_id": principal.caller_id if principal is not None else "",
        "all_projects": bool(principal and principal.all_projects),
        "allowed_project_count": len(principal.allowed_projects) if principal is not None else 0,
    }


@app.get("/bhm/galaxy", response_class=FileResponse)
def bhm_galaxy_view() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "galaxy.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.websocket("/bhm/ws")
async def bhm_memory_pulse_ws(websocket: WebSocket) -> None:
    configured_principal = configured_caller_principal()
    supplied = parse_bearer_token(websocket.headers.get("authorization"))
    bearer_authenticated = configured_principal is not None and is_caller_token_valid(supplied)
    ui_session_token = "" if bearer_authenticated else str(websocket.cookies.get(UI_SESSION_COOKIE) or "")
    ui_session_lease = _UI_SESSIONS.resolve_session_lease(ui_session_token)
    principal = configured_principal if bearer_authenticated else (ui_session_lease[0] if ui_session_lease else None)
    if principal is None:
        await websocket.close(code=4401, reason="caller_auth_required")
        return
    if not _websocket_origin_is_allowed(websocket, require_exact_origin=not bearer_authenticated):
        await websocket.close(code=4403, reason="caller_origin_rejected")
        return

    requested_projects = extract_request_projects(websocket.query_params)
    project_error = authorize_projects(
        principal,
        requested_projects,
        require_explicit=not principal.all_projects,
    )
    if project_error:
        await websocket.close(code=4403, reason=project_error)
        return
    subscribed_projects = None if principal.all_projects and not requested_projects else frozenset(requested_projects)
    await _MEMORY_PULSE_BUS.connect(websocket, subscribed_projects)
    try:
        while True:
            if bearer_authenticated:
                await websocket.receive_text()
                continue
            lease = _UI_SESSIONS.resolve_session_lease(ui_session_token)
            if lease is None:
                await websocket.close(code=4408, reason="ui_session_expired")
                return
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=max(min(lease[1], 1.0), 0.01))
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        _MEMORY_PULSE_BUS.disconnect(websocket)
    finally:
        _MEMORY_PULSE_BUS.disconnect(websocket)


@app.post("/bhm/infra/restart")
async def bhm_infra_restart() -> dict:
    pending = _write_pending_boot_report()
    await _MEMORY_PULSE_BUS.broadcast({"event": "system_cooldown", "timeout": 3000})
    launcher_pid = _spawn_detached_restart_launcher()
    print(f"[INFO] BHM restart launcher detached: pid={launcher_pid}", flush=True)
    os._exit(0)
    return {"ok": True, "launcher_pid": launcher_pid, "boot_report": pending}


@app.post("/mem0/search")
def mem0_search(request: SearchRequest) -> dict:
    try:
        _ensure_provider_warmup_ready_sync()
        project_name = request.project or settings.qdrant_collection
        hits, total = asyncio.run(
            federated_search(
                request.query,
                project_name,
                limit=request.top_k,
                domain=request.domain,
                semantic_type=request.semantic_type,
                priority=request.priority,
                include_archived=request.include_archived,
                include_logs=request.include_logs,
            )
        )
        result: dict = {"results": [_serialize_vector_hit(item) for item in hits], "total": total}
        _emit_memory_pulses_from_mem0_items(result["results"])
        return {
            "ok": True,
            "result": result,
            "filters": {
                "project": request.project,
                "domain": request.domain,
                "semantic_type": request.semantic_type,
                "priority": request.priority,
                "include_archived": request.include_archived,
                "include_logs": request.include_logs,
            },
            "retrieval": {
                "mode": "federated",
                "ranking": "rrf-hybrid",
                "local_collection": local_collection_name(project_name),
                "global_collection": global_collection_name(),
            },
        }
    except Exception as exc:
        if _is_fallback_grace_error(exc):
            return _fallback_grace_mem0_search(request, exc)
        raise


@app.get("/bhm/memory")
def bhm_memory_get(id: str, project: str | None = None) -> dict:
    record = _find_live_memory(id, project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found in live store")
    return {"memory": _serialize_memory_record(record)}


@app.get("/bhm/memories")
async def bhm_memories_list(
    project: str | None = None,
    memory_type: str | None = None,
    include_archived: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    try:
        await _ensure_provider_warmup_ready()
        items = _load_live_memories()
        items = [
            item for item in items
            if _memory_matches_filters(
                item,
                project=project,
                memory_type=memory_type,
                include_archived=include_archived,
            )
        ]

        items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
        total = len(items)
        window = items[max(offset, 0):max(offset, 0) + max(min(limit, 200), 1)]
        return {
            "memories": [_serialize_memory_record(item) for item in window],
            "total": total,
            "limit": max(min(limit, 200), 1),
            "offset": max(offset, 0),
        }
    except Exception as exc:
        if _is_fallback_grace_error(exc):
            return _fallback_grace_memories_response(
                "bhm.memories",
                exc,
                project=project,
                memory_type=memory_type,
                include_archived=include_archived,
                limit=limit,
                offset=offset,
            )
        raise


@app.post("/bhm/memory/update")
async def bhm_memory_update(request: MemoryUpdateRequest) -> dict:
    record = await _run_bounded_write("bhm.memory.update", _update_live_memory, request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/memory/archive")
async def bhm_memory_archive(request: MemoryArchiveRequest) -> dict:
    record = await _run_bounded_write("bhm.memory.archive", _archive_live_memory, request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/forget/preview")
def bhm_forget_preview(request: ForgetPreviewRequest) -> dict:
    return _forget_preview(request)


@app.post("/bhm/forget/apply")
async def bhm_forget_apply(request: ForgetApplyRequest) -> dict:
    return await _run_bounded_write("bhm.forget.apply", _forget_apply, request)


@app.post("/bhm/search/advanced")
async def bhm_search_advanced(request: MemoryAdvancedSearchRequest) -> dict:
    try:
        await _ensure_provider_warmup_ready()
        memories, total = _advanced_search_live_memories(request)
        return {
            "memories": [_serialize_memory_record(item) for item in memories],
            "total": total,
            "limit": max(min(request.limit, 200), 1),
            "offset": max(request.offset, 0),
            "query": request.query,
            "filters": {
                "project": request.project,
                "memory_type": request.memory_type,
                "concepts": request.concepts or [],
                "files": request.files or [],
                "include_archived": request.include_archived,
                "include_logs": request.include_logs,
                "domain": request.domain,
                "semantic_type": request.semantic_type,
                "priority": request.priority,
            },
        }
    except Exception as exc:
        if _is_fallback_grace_error(exc):
            response = _fallback_grace_memories_response(
                "bhm.search.advanced",
                exc,
                project=request.project,
                memory_type=request.memory_type,
                concepts=request.concepts,
                files=request.files,
                query=request.query,
                include_logs=request.include_logs,
                domain=request.domain,
                semantic_type=request.semantic_type,
                priority=request.priority,
                include_archived=request.include_archived,
                limit=request.limit,
                offset=request.offset,
            )
            response["query"] = request.query
            return response
        raise


@app.post("/bhm/search")
async def bhm_search(request: MemoryAdvancedSearchRequest) -> dict:
    if not request.query.strip():
        return await bhm_search_advanced(request)
    try:
        await _ensure_provider_warmup_ready()
        project_name = request.project or settings.qdrant_collection
        hits, total = await federated_search(
            request.query,
            project_name,
            limit=request.limit,
            offset=request.offset,
            memory_type=request.memory_type,
            concepts=request.concepts,
            files=request.files,
            domain=request.domain,
            semantic_type=request.semantic_type,
            priority=request.priority,
            include_archived=request.include_archived,
            include_logs=request.include_logs,
        )
        memories = [_serialize_vector_hit(item) for item in hits]
        _emit_memory_pulses_from_mem0_items(memories)
        if total == 0:
            response = await bhm_search_advanced(request)
            response["retrieval"] = {
                "mode": "federated-empty-live-fallback",
                "local_collection": local_collection_name(project_name),
                "global_collection": global_collection_name(),
            }
            return response
        return {
            "memories": memories,
            "total": total,
            "limit": max(min(request.limit, 200), 1),
            "offset": max(request.offset, 0),
            "query": request.query,
            "filters": {
                "project": request.project,
                "memory_type": request.memory_type,
                "concepts": request.concepts or [],
                "files": request.files or [],
                "include_archived": request.include_archived,
                "include_logs": request.include_logs,
                "domain": request.domain,
                "semantic_type": request.semantic_type,
                "priority": request.priority,
            },
            "retrieval": {
                "mode": "federated",
                "ranking": "rrf-hybrid",
                "local_collection": local_collection_name(project_name),
                "global_collection": global_collection_name(),
            },
        }
    except Exception as exc:
        if _is_fallback_grace_error(exc):
            response = _fallback_grace_memories_response(
                "bhm.search.federated",
                exc,
                project=request.project,
                memory_type=request.memory_type,
                concepts=request.concepts,
                files=request.files,
                query=request.query,
                include_logs=request.include_logs,
                domain=request.domain,
                semantic_type=request.semantic_type,
                priority=request.priority,
                include_archived=request.include_archived,
                limit=request.limit,
                offset=request.offset,
            )
            response["query"] = request.query
            response["retrieval"] = {
                "mode": "federated-fallback-grace",
                "local_collection": local_collection_name(request.project or settings.qdrant_collection),
                "global_collection": global_collection_name(),
            }
            return response
        raise


@app.post("/bhm/context/compile")
async def bhm_context_compile(
    request: ContextCompileRequest,
    http_request: Request,
) -> dict[str, Any]:
    """Compile a bounded, project-scoped context from the canonical retrieval path."""

    await _ensure_provider_warmup_ready()
    try:
        context_profile = resolve_context_profile(request.profile or settings.context_profile, repo_root=settings.repo_root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    effective_limit = request.limit if request.limit is not None else context_profile.limit
    effective_token_budget = request.token_budget if request.token_budget is not None else context_profile.token_budget
    effective_include_archived = request.include_archived or context_profile.include_archived
    effective_include_logs = request.include_logs or context_profile.include_logs
    project_name = _canonical_project(request.project or settings.qdrant_collection)
    candidate_limit = min(max(effective_limit, 20), 50)
    hits, total = await federated_search(
        request.query,
        project_name,
        limit=candidate_limit,
        memory_type=request.memory_type,
        concepts=request.concepts,
        files=request.files,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        include_archived=effective_include_archived,
        include_logs=effective_include_logs,
    )

    # Federated retrieval already applies these filters, but the compiler fails
    # closed once more so a degraded/monkeypatched provider cannot leak another
    # project or archived/log memory into an agent context.
    strict_hits = _strict_retrieval_hits(
        hits,
        project_name=project_name,
        memory_type=request.memory_type,
        concepts=request.concepts,
        files=request.files,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        include_archived=effective_include_archived,
        include_logs=effective_include_logs,
        limit=effective_limit,
    )
    compiled = compile_context(
        [_context_item_from_vector_hit(hit) for hit in strict_hits],
        token_budget=effective_token_budget,
        max_item_chars=context_profile.max_item_chars,
    )
    funnel_snapshot = _RETRIEVAL_FUNNEL.snapshot()
    profile_recommendation = recommend_context_profile(
        request.query,
        requested_profile=request.profile,
        default_profile=settings.context_profile,
        historical_usefulness=summarize_explicit_usefulness(funnel_snapshot, project=project_name),
        filter_count=sum(
            value is not None
            for value in (
                request.memory_type,
                request.concepts,
                request.files,
                request.domain,
                request.semantic_type,
                request.priority,
            )
        ),
    )
    context_confidence = assess_context_confidence(
        hits=strict_hits,
        included_count=int(compiled["included_count"]),
        citations=compiled["citations"],
        total_candidates=total,
        token_budget=int(compiled["token_budget"]),
        omitted_count=int(compiled["omissions"]["count"]),
    )
    funnel_counts = {
        "requested": len(hits),
        "eligible": len(strict_hits),
        "packed": int(compiled["included_count"]),
        "cited": len(compiled["citations"]),
    }
    _RETRIEVAL_FUNNEL.record_context(
        project=project_name,
        profile=context_profile.name,
        surface=_request_surface(http_request),
        requested_count=funnel_counts["requested"],
        eligible_count=funnel_counts["eligible"],
        packed_count=funnel_counts["packed"],
        cited_count=funnel_counts["cited"],
        item_ids=[citation.get("id") for citation in compiled["citations"]],
    )
    return {
        "context": compiled["text"],
        "citations": compiled["citations"],
        "provenance": compiled["provenance"],
        "omissions": compiled["omissions"],
        "query": request.query,
        "project": project_name,
        "profile": context_profile.as_dict(),
        "profile_recommendation": profile_recommendation,
        "context_confidence": context_confidence,
        "retrieval": {
            "mode": "federated",
            "total": total,
            "candidate_count": len(hits),
            "eligible_count": len(strict_hits),
            "included_count": compiled["included_count"],
            "omitted_count": compiled["omissions"]["count"],
            "limit": effective_limit,
            "funnel": funnel_counts,
        },
        "budget": {
            "token_budget": compiled["token_budget"],
            "estimated_tokens": compiled["estimated_tokens"],
            "truncated": compiled["truncated"],
        },
    }


@app.post("/bhm/retrieval/explain")
async def bhm_explain_retrieval(request: RetrievalExplainRequest) -> dict[str, Any]:
    """Explain bounded ranking signals without returning raw retrieval metadata."""

    await _ensure_provider_warmup_ready()
    project_name = _canonical_project(request.project or settings.qdrant_collection)
    candidate_limit = min(max(request.limit, 20), 50)
    hits, total = await federated_search(
        request.query,
        project_name,
        limit=candidate_limit,
        memory_type=request.memory_type,
        concepts=request.concepts,
        files=request.files,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        include_archived=request.include_archived,
        include_logs=request.include_logs,
    )
    strict_hits = _strict_retrieval_hits(
        hits,
        project_name=project_name,
        memory_type=request.memory_type,
        concepts=request.concepts,
        files=request.files,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        include_archived=request.include_archived,
        include_logs=request.include_logs,
        limit=request.limit,
    )
    explanations = [explain_retrieval_hit(hit, rank=index) for index, hit in enumerate(strict_hits, start=1)]
    return {
        "query": request.query,
        "project": project_name,
        "results": explanations,
        "total": total,
        "retrieval": {
            "mode": "federated",
            "ranking": "weighted-fusion+mmr+typed-decay",
            "candidate_count": len(hits),
            "included_count": len(strict_hits),
            "limit": request.limit,
        },
        "filters": {
            "memory_type": request.memory_type,
            "concepts": request.concepts or [],
            "files": request.files or [],
            "include_archived": request.include_archived,
            "include_logs": request.include_logs,
            "domain": request.domain,
            "semantic_type": request.semantic_type,
            "priority": request.priority,
        },
    }


@app.post("/bhm/memory/used")
async def bhm_memory_used(
    request: MemoryUsedRequest,
) -> dict[str, Any]:
    """Record an explicit, bounded access signal for retrieved memories."""

    await _ensure_provider_warmup_ready()
    project_name = _canonical_project(request.project or settings.qdrant_collection)
    requested_ids = list(dict.fromkeys(str(memory_id or "").strip() for memory_id in request.ids))
    requested_ids = [memory_id for memory_id in requested_ids if memory_id]
    fetched_hits: list[dict] = []
    missing_ids: list[str] = []
    for memory_id in requested_ids:
        hit = await _fetch_qdrant_hit_by_source_id(memory_id, project_name)
        if hit is None:
            missing_ids.append(memory_id)
            continue
        strict_hits = _strict_retrieval_hits(
            [hit],
            project_name=project_name,
            include_archived=False,
            include_logs=False,
            limit=1,
        )
        if not strict_hits:
            missing_ids.append(memory_id)
            continue
        fetched_hits.extend(strict_hits)

    accessed_at = _utc_now_iso()
    updates = _access_updates_for_hits(fetched_hits, accessed_at)
    if updates:
        _schedule_vector_access_updates(fetched_hits)
    used_ids = [
        str((hit.get("metadata") or {}).get("source_id") or hit.get("source_id") or hit.get("id") or "")
        for hit in fetched_hits
    ]
    funnel_matched_count = _RETRIEVAL_FUNNEL.record_memory_used(
        project=project_name,
        item_ids=used_ids,
    )
    return {
        "success": True,
        "project": project_name,
        "requested_count": len(requested_ids),
        "used_count": len(used_ids),
        "scheduled_count": len(updates),
        "used_ids": list(dict.fromkeys(memory_id for memory_id in used_ids if memory_id)),
        "missing_ids": missing_ids,
        "accessed_at": accessed_at,
        "reason": request.reason,
        "funnel_matched_count": funnel_matched_count,
    }


@app.post("/bhm/recent-activity")
def bhm_recent_activity(request: MemoryRecentActivityRequest) -> dict:
    memories = _recent_activity_live_memories(request)
    return {
        "memories": [_serialize_memory_record(item) for item in memories],
        "limit": max(min(request.limit, 200), 1),
        "filters": {
            "project": request.project,
            "memory_type": request.memory_type,
            "include_archived": request.include_archived,
        },
    }


@app.post("/bhm/memory/upsert")
async def bhm_memory_upsert(request: MemoryUpsertRequest) -> dict:
    action, record = await _run_bounded_write("bhm.memory.upsert", _upsert_live_memory, request)
    semantic_graph = await _add_semantic_dependency_links(record, request.project)
    return {
        "success": True,
        "action": action,
        "memory": _serialize_memory_record(record),
        "semantic_graph": semantic_graph,
    }


@app.post("/bhm/crystallize")
async def bhm_crystallize(request: MemoryCrystallizeRequest) -> dict:
    action, record = await _run_bounded_write("bhm.crystallize", _crystallize_memories, request)
    return {"success": True, "action": action, "memory": _serialize_memory_record(record)}


@app.post("/bhm/hooks/compact", status_code=202)
async def bhm_hook_compact(request: BhmHookCompactRequest) -> dict:
    secured_request = _secure_observation_request_model(
        request,
        max_input_bytes=OBSERVATION_COMPACT_MAX_INPUT_BYTES,
    )
    _, response = await _enqueue_hook_request("compact", secured_request)
    return response


@app.post("/bhm/hooks/idle", status_code=202)
async def bhm_hook_idle(request: BhmHookIdleRequest) -> dict:
    secured_request = _secure_observation_request_model(
        request,
        max_input_bytes=OBSERVATION_IDLE_MAX_INPUT_BYTES,
    )
    _, response = await _enqueue_hook_request("idle", secured_request)
    return response


@app.get("/bhm/hooks/queue/status")
def bhm_hook_queue_status(integrity: bool = False) -> dict:
    status = _hook_queue().status(integrity_check=integrity)
    active_worker_tasks = sum(1 for task in _HOOK_QUEUE_TASKS if not task.done())
    status.update(
        {
            "accepting": _HOOK_QUEUE_ACCEPTING,
            "workerTasks": active_worker_tasks,
            "workerTasksConfigured": _HOOK_COMPACT_WORKERS + _HOOK_IDLE_WORKERS,
            "workerTasksStopped": len(_HOOK_QUEUE_TASKS) - active_worker_tasks,
            "compactWorkers": _HOOK_COMPACT_WORKERS,
            "idleWorkers": _HOOK_IDLE_WORKERS,
            "leaseSeconds": _HOOK_QUEUE_LEASE_SECONDS,
            "drainSeconds": _HOOK_QUEUE_DRAIN_SECONDS,
        }
    )
    return status


@app.get("/bhm/hooks/jobs/{job_id}")
def bhm_hook_job_status(job_id: str) -> dict:
    job = _hook_queue().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "hook_job_not_found", "jobId": job_id})
    return {"job": job}


@app.post("/bhm/synthesis/fact-crystal")
async def bhm_synthesis_fact_crystal(request: FactSynthesisRequest) -> dict:
    try:
        fact, tokens = await _call_fact_synthesis_llm(request)
        mode = "llm"
        fallback_reason = ""
    except Exception:
        if _configured_fallback_mode() == "disabled":
            raise
        fact = _fallback_fact_synthesis(request, "provider_unavailable")
        tokens = {"prompt": 0, "completion": 0, "total": 0}
        mode = "fallback"
        fallback_reason = "provider_unavailable"

    crystal_metadata = initial_decay_metadata({"importance_score": fact.get("importance_score")}, created_at=_utc_now_iso())
    return {
        "ok": True,
        "project_name": request.project_name,
        "session_id": request.session_id,
        "fact_crystal": fact,
        "crystal": _format_fact_synthesis_crystal(fact),
        "crystal_metadata": crystal_metadata,
        "synthesis": {
            "mode": mode,
            "degraded": mode == "fallback",
            "provider": "local-lm-studio",
            "model": settings.mem0_llm_model,
            "fallback_reason": fallback_reason,
            "tokens": tokens,
        },
    }


@app.post("/bhm/checkpoint")
async def bhm_checkpoint_create(request: CheckpointCreateRequest) -> dict:
    action, checkpoint = await _run_bounded_write("bhm.checkpoint", _create_checkpoint, request)
    return {"success": True, "action": action, "checkpoint": _serialize_checkpoint_record(checkpoint)}


@app.get("/bhm/checkpoints")
def bhm_checkpoint_list(
    project: str | None = None,
    checkpoint_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    checkpoints, total = _list_checkpoints(project, checkpoint_type, limit, offset)
    return {
        "checkpoints": [_serialize_checkpoint_record(item) for item in checkpoints],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


@app.get("/bhm/checkpoint/latest")
def bhm_checkpoint_get_latest(project: str, checkpoint_type: str | None = None) -> dict:
    checkpoint = _get_latest_checkpoint(project, checkpoint_type)
    return {"checkpoint": _serialize_checkpoint_record(checkpoint)}


@app.get("/bhm/project-map")
def bhm_project_map_get(project: str) -> dict:
    project_map = _get_project_map(project)
    return {"project_map": _serialize_project_map_record(project_map)}


@app.get("/bhm/projects")
def bhm_projects(request: Request) -> dict:
    report = _PROJECT_REGISTRY.report()
    principal = getattr(request.state, "bhm_caller_principal", None)
    if principal is None or principal.all_projects:
        return report
    allowed = set(principal.allowed_projects)
    report["projects"] = [item for item in report["projects"] if str(item.get("id") or "") in allowed]
    if principal.default_project not in allowed:
        report["default_project"] = next(iter(sorted(allowed)), "")
    return report


@app.get("/bhm/project/resolve")
def bhm_project_resolve(project: str = "") -> dict:
    resolution: ProjectResolution = _PROJECT_REGISTRY.resolve(project)
    return {"resolution": resolution.as_dict()}


@app.get("/bhm/project/retirement-preview")
def bhm_project_retirement_preview(project: str) -> dict:
    """Read-only, preview-first project retirement contract."""

    project_id = _canonical_project(project)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    try:
        return preview_project_retirement(database_path, project_id)
    except ProjectRetirementError as exc:
        raise HTTPException(status_code=422, detail={"error": "project_retirement_preview_rejected", "detail": str(exc)[:500]}) from exc


@app.post("/bhm/project/retirement/apply")
async def bhm_project_retirement_apply(request: ProjectRetirementRequest, http_request: Request) -> dict:
    """Apply an allowlisted logical retirement; never unlinks a database."""

    project_id = _canonical_project(request.project)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    capability = str(request.capability or http_request.headers.get(ADMIN_CAPABILITY_HEADER, ""))

    def _apply() -> dict[str, Any]:
        try:
            return apply_project_retirement(
                database_path,
                project_id,
                capability=capability,
                backup_dir=request.backup_dir,
            )
        except ProjectRetirementError as exc:
            raise HTTPException(status_code=422, detail={"error": "project_retirement_rejected", "detail": str(exc)[:500]}) from exc

    return await _run_bounded_write("bhm.project-retirement", _apply)


@app.post("/bhm/project-map")
async def bhm_project_map_upsert(request: ProjectMapUpsertRequest) -> dict:
    action, project_map = await _run_bounded_write("bhm.project-map", _upsert_project_map, request)
    return {"success": True, "action": action, "project_map": _serialize_project_map_record(project_map)}


@app.post("/bhm/memory/merge")
async def bhm_memory_merge(request: MemoryMergeRequest) -> dict:
    result = await _run_bounded_write("bhm.memory.merge", _merge_memories, request)
    return {
        "success": True,
        "target": _serialize_memory_record(result["target"]),
        "source": _serialize_memory_record(result["source"]),
        "archived_source": result["archived_source"],
    }


@app.post("/bhm/memory/detect-duplicates")
def bhm_memory_detect_duplicates(request: MemoryDetectRequest) -> dict:
    duplicates = _detect_duplicates(request)
    return {"duplicates": [_serialize_duplicate_candidate(item) for item in duplicates], "limit": max(min(request.limit, 200), 1)}


@app.post("/bhm/memory/detect-conflicts")
def bhm_memory_detect_conflicts(request: MemoryDetectRequest) -> dict:
    conflicts = _detect_conflicts(request)
    return {"conflicts": [_serialize_conflict_candidate(item) for item in conflicts], "limit": max(min(request.limit, 200), 1)}


@app.post("/bhm/memory/lint")
def bhm_memory_lint(request: MemoryLintRequest) -> dict:
    return _lint_memory(request)


@app.delete("/bhm/memory")
async def bhm_memory_delete(request: MemoryDeleteRequest) -> dict:
    deleted = await _run_bounded_write("bhm.memory.delete", _delete_live_memory, request)
    return {"success": True, "memory": _serialize_memory_record(deleted)}


@app.delete("/bhm/memory/hard")
async def bhm_memory_delete_hard(request: HardDeleteMemoryRequest) -> dict:
    deleted = await _run_bounded_write("bhm.memory.delete-hard", _delete_live_memory_hard, request)
    return {"success": True, "memory": _serialize_memory_record(deleted)}


@app.post("/bhm/memories/batch-archive")
async def bhm_memories_batch_archive(request: BatchMemoryIdsRequest) -> dict:
    return await _run_bounded_write("bhm.memories.batch-archive", _batch_archive_memories, request)


@app.post("/bhm/memories/batch-delete")
async def bhm_memories_batch_delete(request: BatchMemoryIdsRequest) -> dict:
    return await _run_bounded_write("bhm.memories.batch-delete", _batch_delete_memories, request)


@app.post("/bhm/memories/batch-link")
async def bhm_memories_batch_link(request: BatchLinkMemoriesRequest) -> dict:
    return await _run_bounded_write("bhm.memories.batch-link", _batch_link_memories, request)


@app.post("/bhm/memories/batch-unlink")
async def bhm_memories_batch_unlink(request: BatchMemoryIdsRequest) -> dict:
    return await _run_bounded_write("bhm.memories.batch-unlink", _batch_unlink_memories, request)


@app.get("/bhm/memories/by-concept")
def bhm_memories_by_concept(
    concept: str,
    project: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    request = MemoryAdvancedSearchRequest(
        query="",
        project=project,
        concepts=[concept],
        limit=limit,
        offset=offset,
    )
    memories, total = _advanced_search_live_memories(request)
    return {
        "memories": [_serialize_memory_record(item) for item in memories],
        "total": total,
        "concept": concept,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


@app.get("/bhm/memories/by-type")
def bhm_memories_by_type(
    memory_type: str,
    project: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    items = [
        item for item in _load_live_memories()
        if _memory_matches_filters(item, project=project, memory_type=memory_type, include_archived=False)
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    window = items[max(offset, 0):max(offset, 0) + max(min(limit, 200), 1)]
    return {
        "memories": [_serialize_memory_record(item) for item in window],
        "total": total,
        "memory_type": memory_type,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


@app.post("/bhm/memory/confidence")
async def bhm_memory_set_confidence(request: MemoryConfidenceRequest) -> dict:
    record = await _run_bounded_write("bhm.memory.confidence", _set_memory_confidence, request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/memory/pin")
async def bhm_memory_pin(request: MemoryPinRequest) -> dict:
    record = await _run_bounded_write("bhm.memory.pin", _set_memory_pin, request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/memory/vote-quality")
async def bhm_memory_vote_quality(request: MemoryVoteRequest) -> dict:
    record = await _run_bounded_write("bhm.memory.vote-quality", _vote_memory_quality, request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.get("/bhm/memories/pinned")
def bhm_memories_pinned(
    project: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    memories, total = _list_pinned_memories(project=project, limit=limit, offset=offset)
    return {
        "memories": [_serialize_memory_record(item) for item in memories],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


@app.get("/bhm/memories/archived")
def bhm_memories_archived(project: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    memories, total = _list_archived_memories(project, limit, offset)
    return {
        "memories": [_serialize_memory_record(item) for item in memories],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


@app.post("/bhm/adr")
async def bhm_adr_create(request: AdrCreateRequest) -> dict:
    action, record = await _run_bounded_write("bhm.adr", _create_adr, request)
    return {"success": True, "action": action, "adr": _serialize_adr_record(record)}


@app.get("/bhm/adrs")
def bhm_adr_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    items, total = _list_adrs(project, limit, offset)
    return {
        "adrs": [_serialize_adr_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


@app.post("/bhm/adr/supersede")
async def bhm_adr_supersede(project: str, old_id: str, new_id: str) -> dict:
    result = await _run_bounded_write("bhm.adr.supersede", _adr_supersede, project, old_id, new_id)
    return {
        "success": True,
        "old": _serialize_adr_record(result["old"]),
        "new": _serialize_adr_record(result["new"]),
    }


@app.post("/bhm/handoff")
async def bhm_handoff_create(request: HandoffCreateRequest) -> dict:
    action, record = await _run_bounded_write("bhm.handoff", _create_handoff, request)
    return {"success": True, "action": action, "handoff": _serialize_handoff_record(record)}


@app.get("/bhm/handoffs")
def bhm_handoff_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    items, total = _list_handoffs(project, limit, offset)
    return {
        "handoffs": [_serialize_handoff_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


@app.post("/bhm/session-record")
async def bhm_session_record_create(request: SessionRecordCreateRequest) -> dict:
    action, record = await _run_bounded_write("bhm.session-record", _create_session_record, request)
    return {"success": True, "action": action, "session_record": _serialize_session_record(record)}


@app.get("/bhm/session-records")
def bhm_session_record_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    items, total = _list_session_records(project, limit, offset)
    return {
        "session_records": [_serialize_session_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


@app.post("/bhm/task/open")
async def bhm_task_open(request: TaskOpenRequest) -> dict:
    action, record = await _run_bounded_write("bhm.task.open", _open_task, request)
    return {"success": True, "action": action, "task": _serialize_task_record(record)}


@app.post("/bhm/task/close")
async def bhm_task_close(request: TaskCloseRequest) -> dict:
    action, record = await _run_bounded_write("bhm.task.close", _close_task, request)
    return {"success": True, "action": action, "task": _serialize_task_record(record)}


@app.get("/bhm/task")
def bhm_task_get(task_id: str, project: str | None = None) -> dict:
    return {"task": _serialize_task_record(_get_task(task_id, project))}


@app.get("/bhm/tasks")
def bhm_task_list(
    project: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    items, total = _list_tasks(project, status, limit, offset)
    return {
        "tasks": [_serialize_task_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


@app.post("/bhm/task-context")
async def bhm_task_context_update(request: TaskContextUpdateRequest) -> dict:
    action, record = await _run_bounded_write("bhm.task-context", _upsert_task_context, request)
    return {"success": True, "action": action, "task_context": _serialize_task_context_record(record)}


@app.get("/bhm/task-context")
def bhm_task_context_get(project: str) -> dict:
    record = _get_task_context(project)
    return {"task_context": _serialize_task_context_record(record)}


@app.post("/bhm/risk-register")
async def bhm_risk_register_update(request: RiskRegisterUpdateRequest) -> dict:
    action, record = await _run_bounded_write("bhm.risk-register", _upsert_risk_register, request)
    return {"success": True, "action": action, "risk_register": _serialize_risk_register_record(record)}


@app.get("/bhm/risk-register")
def bhm_risk_register_get(project: str) -> dict:
    record = _get_risk_register(project)
    return {"risk_register": _serialize_risk_register_record(record)}


@app.post("/bhm/validation-snapshot")
async def bhm_validation_snapshot_save(request: ValidationSnapshotSaveRequest) -> dict:
    action, record = await _run_bounded_write("bhm.validation-snapshot", _save_validation_snapshot_record, request)
    return {"success": True, "action": action, "validation_snapshot": _serialize_validation_snapshot_record(record)}


@app.get("/bhm/validation-snapshot")
def bhm_validation_snapshot_get(project: str) -> dict:
    record = _get_validation_snapshot(project)
    return {"validation_snapshot": _serialize_validation_snapshot_record(record)}


@app.post("/bhm/memory/source-refs")
async def bhm_memory_source_refs_attach(request: MemorySourceRefsRequest) -> dict:
    record = await _run_bounded_write("bhm.memory.source-refs", _attach_source_refs, request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.get("/bhm/memory/source-refs")
def bhm_memory_source_refs_get(id: str, project: str | None = None) -> dict:
    return _get_source_refs(id, project)


@app.post("/bhm/memory/timeline")
def bhm_memory_timeline(request: MemoryTimelineRequest) -> dict:
    items = _memory_timeline(request)
    return {
        "timeline": [_serialize_memory_record(item) for item in items],
        "limit": max(min(request.limit, 200), 1),
        "filters": {
            "project": request.project,
            "concept": request.concept,
            "memory_type": request.memory_type,
            "include_archived": request.include_archived,
        },
    }


@app.get("/bhm/query-suggestions")
def bhm_query_suggestions(project: str | None = None) -> dict:
    return {"suggestions": _query_suggestions(project)}


@app.post("/bhm/memory/source-refs/replace")
async def bhm_memory_source_refs_replace(request: SourceRefsReplaceRequest) -> dict:
    record = await _run_bounded_write("bhm.memory.source-refs.replace", _replace_source_refs, request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/memory/source-refs/detach")
async def bhm_memory_source_refs_detach(request: SourceRefsDetachRequest) -> dict:
    record = await _run_bounded_write("bhm.memory.source-refs.detach", _detach_source_refs, request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/memory/restore")
async def bhm_memory_restore(request: RestoreMemoryRequest) -> dict:
    record = await _run_bounded_write("bhm.memory.restore", _restore_archived_memory, request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/memories/batch-upsert")
async def bhm_memories_batch_upsert(request: BatchUpsertMemoriesRequest) -> dict:
    return await _run_bounded_write("bhm.memories.batch-upsert", _batch_upsert_memories, request)


@app.post("/bhm/memory/source-refs/batch")
async def bhm_memory_source_refs_batch(request: BatchAttachSourceRefsRequest) -> dict:
    return await _run_bounded_write("bhm.memory.source-refs.batch", _batch_attach_source_refs, request)


@app.post("/bhm/integrity-audit")
def bhm_integrity_audit(project: str | None = None) -> dict:
    return _integrity_audit(project)


@app.get("/bhm/audit")
def bhm_audit(project: str = "e-github-workspace", operation: str = "", limit: int = 50) -> dict:
    return {
        "project": project,
        "operation": operation,
        "limit": max(min(limit, 200), 1),
        "audit": _integrity_audit(project),
    }


@app.post("/bhm/artifact-integrity-audit")
def bhm_artifact_integrity_audit(request: ProjectOnlyRequest) -> dict:
    return _artifact_integrity_audit(request.project)


@app.post("/bhm/repair-live-indexes")
def bhm_repair_live_indexes(request: RepairLiveIndexesRequest) -> dict:
    return _repair_live_indexes(request)


@app.post("/bhm/project-summary/rebuild")
def bhm_rebuild_project_summary(request: RebuildProjectSummaryRequest) -> dict:
    return _rebuild_project_summary(request)


@app.get("/bhm/project-summary")
def bhm_project_summary_get(project: str) -> dict:
    return {"memory": _project_summary_get(project)}


@app.post("/bhm/project-summary/pin")
def bhm_project_summary_pin(request: ProjectSummaryPinRequest) -> dict:
    record = _project_summary_pin(request.project)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/project-summary/list")
def bhm_project_summary_list(request: ProjectSummaryListRequest) -> dict:
    items, total = _project_summary_list(request)
    return {"memories": [_serialize_memory_record(item) for item in items], "total": total, "limit": max(min(request.limit, 200), 1), "offset": max(request.offset, 0)}


@app.post("/bhm/entity-extract")
def bhm_entity_extract(request: EntityExtractRequest) -> dict:
    return _entity_extract(request)


@app.post("/bhm/relation-suggest")
def bhm_relation_suggest(request: RelationSuggestRequest) -> dict:
    return _relation_suggest(request)


@app.post("/bhm/memory/diff")
def bhm_memory_diff(request: MemoryDiffRequest) -> dict:
    return _memory_diff(request)


@app.post("/bhm/memory/compact")
def bhm_memory_compact(request: MemoryCompactRequest) -> dict:
    record = _compact_memory(request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/link-graph-stats")
def bhm_link_graph_stats(request: ProjectOnlyRequest) -> dict:
    return _link_graph_stats(request.project)


@app.post("/bhm/reindex-memory-metadata")
def bhm_reindex_memory_metadata(request: ProjectOnlyRequest) -> dict:
    return _reindex_memory_metadata(request.project)


@app.post("/bhm/memory/schema-validate")
def bhm_memory_schema_validate(request: MemorySchemaValidateRequest) -> dict:
    return _memory_schema_validate(request.id, request.project)


@app.post("/bhm/memory/type-migrate")
def bhm_memory_type_migrate(request: TypeMigrateRequest) -> dict:
    record = _memory_type_migrate(request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/search/hybrid")
def bhm_search_hybrid(request: HybridSearchRequest) -> dict:
    return _search_hybrid(request)


@app.post("/bhm/search/by-source-ref")
def bhm_search_by_source_ref(request: SearchByRefRequest) -> dict:
    return _search_by_source_ref(request)


@app.post("/bhm/search/by-upsert-key")
def bhm_search_by_upsert_key(request: SearchByUpsertKeyRequest) -> dict:
    return _search_by_upsert_key(request)


@app.post("/bhm/memory/restore-batch")
def bhm_memory_restore_batch(request: BatchRestoreRequest) -> dict:
    return _batch_restore_memories(request)


@app.post("/bhm/artifact/restore")
def bhm_artifact_restore(request: ArtifactRestoreRequest) -> dict:
    return _artifact_restore(request)


@app.post("/bhm/artifact/relink")
def bhm_orphan_artifact_relink(request: OrphanArtifactRelinkRequest) -> dict:
    return _orphan_artifact_relink(request)


@app.post("/bhm/memory/staleness-report")
def bhm_memory_staleness_report(request: MemoryStalenessReportRequest) -> dict:
    return _memory_staleness_report(request)


@app.post("/bhm/memory/review-queue")
def bhm_memory_review_queue(request: MemoryReviewQueueRequest) -> dict:
    return _memory_review_queue(request)


@app.post("/bhm/memory/triage-queue")
def bhm_memory_triage_queue(request: MemoryTriageQueueRequest) -> dict:
    return _memory_triage_queue(request)


@app.post("/bhm/project-summary/refresh-all")
def bhm_project_summary_refresh_all(request: ProjectSummaryRefreshAllRequest) -> dict:
    return _project_summary_refresh_all(request)


@app.post("/bhm/relation/apply-suggestions")
def bhm_relation_apply_suggestions(request: RelationApplySuggestionsRequest) -> dict:
    return _relation_apply_suggestions(request)


@app.post("/bhm/memory/merge-preview")
def bhm_memory_merge_preview(request: MemoryMergePreviewRequest) -> dict:
    return _memory_merge_preview(request)


@app.post("/bhm/schema/upgrade-all")
def bhm_schema_upgrade_all(request: SchemaUpgradeAllRequest) -> dict:
    return _schema_upgrade_all(request)


@app.post("/bhm/memory/redact")
def bhm_memory_redact(request: MemoryRedactRequest) -> dict:
    return _memory_redact(request)


@app.post("/bhm/memory/secret-scan")
def bhm_secret_scan_existing_memories(request: SecretScanRequest) -> dict:
    return _secret_scan_existing_memories(request)


@app.post("/bhm/agent-activity-rollup")
def bhm_agent_activity_rollup(request: ProjectOnlyRequest) -> dict:
    return _agent_activity_rollup(request.project)


@app.post("/bhm/project-memory-heatmap")
def bhm_project_memory_heatmap(request: ProjectOnlyRequest) -> dict:
    return _project_memory_heatmap(request.project)


@app.post("/bhm/relation/confidence")
def bhm_relation_confidence_set(request: RelationConfidenceRequest) -> dict:
    link = _relation_confidence_set(request)
    return {"success": True, "link": link}


@app.post("/bhm/relation/vote-quality")
def bhm_relation_vote_quality(request: RelationVoteRequest) -> dict:
    link = _relation_vote_quality(request)
    return {"success": True, "link": link}


@app.post("/bhm/memory/alias/add")
def bhm_memory_alias_add(request: MemoryAliasRequest) -> dict:
    record = _memory_alias_add(request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/memory/alias/remove")
def bhm_memory_alias_remove(request: MemoryAliasRequest) -> dict:
    record = _memory_alias_remove(request)
    return {"success": True, "memory": _serialize_memory_record(record)}


@app.post("/bhm/memory/alias/resolve")
def bhm_alias_resolve(request: AliasResolveRequest) -> dict:
    return _alias_resolve(request)


@app.post("/bhm/entity-catalog/get")
def bhm_entity_catalog_get(request: EntityCatalogRequest) -> dict:
    return _entity_catalog_get(request.project)


@app.post("/bhm/entity-catalog/rebuild")
def bhm_entity_catalog_rebuild(request: EntityCatalogRequest) -> dict:
    return _entity_catalog_rebuild(request.project)


@app.post("/bhm/project-summary/compare")
def bhm_project_summary_compare(request: ProjectSummaryCompareRequest) -> dict:
    return _project_summary_compare(request)


@app.post("/bhm/memory/usage-stats")
def bhm_memory_usage_stats(request: ProjectOnlyRequest) -> dict:
    return _memory_usage_stats(request.project)


@app.post("/bhm/recent-failures-feed")
def bhm_recent_failures_feed(request: RecentFailuresFeedRequest) -> dict:
    return _recent_failures_feed(request)


@app.post("/bhm/memory/restore-hard-deleted-preview")
def bhm_memory_restore_hard_deleted_preview(request: HardDeleteRestorePreviewRequest) -> dict:
    return _memory_restore_hard_deleted_preview(request)


@app.post("/bhm/artifact/delete")
def bhm_artifact_delete(request: ArtifactDeleteRequest) -> dict:
    return _artifact_delete(request)


@app.post("/bhm/artifact/list-by-type")
def bhm_artifact_list_by_type(request: ArtifactListRequest) -> dict:
    return _artifact_list_by_type(request)


@app.post("/bhm/artifact/usage-stats")
def bhm_artifact_usage_stats(request: ProjectOnlyRequest) -> dict:
    return _artifact_usage_stats(request.project)


@app.post("/bhm/memory/gc-candidates")
def bhm_memory_gc_candidates(request: MemoryGcCandidatesRequest) -> dict:
    return _memory_gc_candidates(request)


@app.post("/bhm/memory/compaction-report")
def bhm_memory_compaction_report(request: MemoryCompactionReportRequest) -> dict:
    return _memory_compaction_report(request)


@app.post("/bhm/link/cycle-detect")
def bhm_link_cycle_detect(request: LinkCycleDetectRequest) -> dict:
    return _link_cycle_detect(request)


@app.post("/bhm/link/orphan-scan")
def bhm_link_orphan_scan(request: ProjectOnlyRequest) -> dict:
    return _link_orphan_scan(request.project)


@app.post("/bhm/project-map/compare")
def bhm_project_map_compare(request: ProjectMapCompareRequest) -> dict:
    return _project_map_compare(request)


@app.post("/bhm/validation/trend-report")
def bhm_validation_trend_report(request: ValidationTrendReportRequest) -> dict:
    return _validation_trend_report(request)


@app.post("/bhm/entity/search")
def bhm_entity_search(request: EntitySearchRequest) -> dict:
    return _entity_search(request)


@app.post("/bhm/entity/link-memories")
def bhm_entity_link_memories(request: EntityLinkMemoriesRequest) -> dict:
    return _entity_link_memories(request)


@app.post("/bhm/alias/stats")
def bhm_alias_stats(request: ProjectOnlyRequest) -> dict:
    return _alias_stats(request.project)


@app.post("/bhm/relation/prune-low-quality")
def bhm_relation_prune_low_quality(request: RelationPruneLowQualityRequest) -> dict:
    return _relation_prune_low_quality(request)


@app.post("/bhm/project-similarity-report")
def bhm_project_similarity_report(request: ProjectSimilarityReportRequest) -> dict:
    return _project_similarity_report(request)


@app.post("/bhm/memory/changelog")
def bhm_memory_changelog(request: MemoryChangelogRequest) -> dict:
    return _memory_changelog(request)


@app.post("/bhm/review-queue/apply")
def bhm_review_queue_apply(request: ReviewQueueApplyRequest) -> dict:
    return _review_queue_apply(request)


@app.post("/bhm/triage-queue/apply")
def bhm_triage_queue_apply(request: TriageQueueApplyRequest) -> dict:
    return _triage_queue_apply(request)


@app.post("/bhm/artifact/batch-delete")
def bhm_artifact_batch_delete(request: ArtifactBatchDeleteRequest) -> dict:
    return _artifact_batch_delete(request)


@app.post("/bhm/artifact/batch-relink")
def bhm_artifact_batch_relink(request: ArtifactBatchRelinkRequest) -> dict:
    return _artifact_batch_relink(request)


@app.post("/bhm/artifact/batch-restore")
def bhm_artifact_batch_restore(request: ArtifactBatchRestoreRequest) -> dict:
    return _artifact_batch_restore(request)


@app.post("/bhm/schema/validate-strict")
def bhm_schema_validate_strict(request: StrictSchemaValidateRequest) -> dict:
    return _schema_validate_strict(request)


@app.post("/bhm/integrity/repair-strict")
def bhm_integrity_repair_strict(request: IntegrityRepairStrictRequest) -> dict:
    return _integrity_repair_strict(request)


@app.post("/bhm/memory/normalize-metadata")
def bhm_memory_normalize_metadata(request: ProjectOnlyRequest) -> dict:
    return _normalize_memory_metadata(request.project)


@app.post("/bhm/admin/export")
def bhm_admin_export(request: AdminExportRequest) -> dict:
    return _admin_export(request)


@app.post("/bhm/admin/import-preview")
def bhm_admin_import_preview(request: AdminImportPreviewRequest) -> dict:
    return _admin_import_preview(request)


@app.post("/bhm/admin/import-apply")
def bhm_admin_import_apply(request: AdminImportApplyRequest) -> dict:
    return _admin_import_apply(request)


@app.get("/bhm/policy/profile")
async def bhm_policy_profile_get() -> dict:
    snapshot = await asyncio.to_thread(_load_policy_profile_snapshot)
    return snapshot["profile"]


@app.post("/bhm/policy/profile")
async def bhm_policy_profile_set(request: PolicyProfileSetRequest) -> dict:
    return await asyncio.to_thread(_policy_profile_set, request)


@app.post("/bhm/policy/enforce-memory")
def bhm_policy_enforce_memory(request: PolicyEnforceMemoryRequest) -> dict:
    return _policy_enforce_memory(request)


@app.post("/bhm/overlap/report")
def bhm_overlap_report(request: OverlapReportRequest) -> dict:
    return _overlap_report(request)


@app.post("/bhm/overlap/cleanup-apply")
def bhm_overlap_cleanup_apply(request: OverlapCleanupApplyRequest) -> dict:
    return _overlap_cleanup_apply(request)


@app.post("/bhm/policy-guard")
def bhm_policy_guard(request: PolicyGuardRequest) -> dict:
    return _policy_guard(request)


@app.post("/bhm/reflect")
def bhm_reflect(request: ReflectRequest) -> dict:
    return {"success": True, "reflection": _build_reflection(request.project, request.maxClusters)}


@app.post("/bhm/insights/search")
def bhm_insights_search(request: BhmMatchSearchRequest) -> dict:
    result = _mem0_match_search(request)
    insights = [
        {
            "title": item.get("title"),
            "source_id": item.get("source_id"),
            "type": item.get("type"),
        }
        for item in result.get("matches", [])
    ]
    response = {"insights": insights, "count": len(insights)}
    if result.get("fallback_grace"):
        response["fallback_grace"] = result["fallback_grace"]
    return response


@app.get("/bhm/lessons")
def bhm_lessons_list(project: str = "e-github-workspace", minConfidence: float = 0.0, limit: int = 10) -> dict:
    return {"lessons": _list_lessons(project, minConfidence, limit)}


@app.post("/bhm/lessons/strengthen")
def bhm_lessons_strengthen(request: LessonStrengthenRequest) -> dict:
    return {"success": True, "lesson": _strengthen_lesson(request)}


@app.post("/bhm/verify")
def bhm_verify(request: MemoryVerifyRequest) -> dict:
    return _verify_memory(request)


@app.get("/bhm/memory/links")
def bhm_memory_links_get(id: str, project: str) -> dict:
    links = _get_memory_links(id, project)
    return {"links": [_serialize_memory_link(item) for item in links]}


@app.post("/bhm/memory/link")
async def bhm_memory_link_create(request: MemoryLinkRequest) -> dict:
    link = await _run_bounded_write("bhm.memory.link", _create_memory_link, request)
    return {"success": True, "link": _serialize_memory_link(link)}


@app.delete("/bhm/memory/link")
async def bhm_memory_link_delete(request: MemoryLinkDeleteRequest) -> dict:
    deleted = await _run_bounded_write("bhm.memory.link.delete", _delete_memory_link, request)
    return {"success": True, "deleted": deleted}


@app.get("/bhm/profile")
async def bhm_profile(project: str = "e-github-workspace") -> dict:
    memory_records, policy_snapshot, registry_snapshot = await asyncio.gather(
        asyncio.to_thread(_fallback_memory_records, project=project, include_logs=True),
        asyncio.to_thread(_load_policy_profile_snapshot),
        asyncio.to_thread(_load_mcp_registry_snapshot),
    )
    policy_profile = policy_snapshot["profile"]
    provider_warmup = _get_provider_warmup_status()
    mem0_plan = mem0_runtime_plan()
    provider_ready = bool(provider_warmup.get("ready"))
    mem0_enabled = bool(mem0_plan.get("enabled"))
    if not mem0_enabled:
        status = "disabled"
    elif not provider_ready:
        status = "warming"
    elif not policy_snapshot["exists"] or not registry_snapshot["loaded"]:
        status = "degraded"
    else:
        status = "ready"
    ready = status == "ready"

    response = {
        "status": status,
        "context_flags": {
            "project": project,
            "include_logs": True,
            "policy_profile_loaded": bool(policy_snapshot["exists"]),
            "registry_loaded": bool(registry_snapshot["loaded"]),
            "provider_ready": provider_ready,
            "mem0_enabled": mem0_enabled,
            "require_project": bool(policy_profile.get("require_project")),
            "require_memory_type": bool(policy_profile.get("require_memory_type")),
            "block_secret_like": bool(policy_profile.get("block_secret_like")),
            "block_raw_logs": bool(policy_profile.get("block_raw_logs")),
        },
        "readiness": {
            "ready": ready,
            "provider_warmup": provider_warmup,
            "mem0": mem0_plan,
            "registry": {
                "path": registry_snapshot["path"],
                "loaded": bool(registry_snapshot["loaded"]),
                "instance_count": registry_snapshot["instance_count"],
            },
            "policy_profile": {
                "path": policy_snapshot["path"],
                "loaded": bool(policy_snapshot["exists"]),
            },
        },
        "profile": {
            "project": project,
            "sessionCount": len(memory_records),
            "memoryCount": len(memory_records),
            "source": "live-memory-snapshot",
        },
    }
    return response


@app.post("/bhm/diagnostics")
def bhm_diagnostics() -> dict:
    report = dependency_report()
    return {
        "ok": report["ok"],
        "service": settings.app_name,
        "dependencies": report["dependencies"],
    }


@app.get("/bhm/insights")
def bhm_insights(project: str = "e-github-workspace", limit: int = 5) -> dict:
    search_response = mem0_search(
        SearchRequest(
            query=f"{project} checkpoint",
            project=project,
            user_id=settings.mem0_user_id,
            top_k=limit,
            include_logs=True,
        )
    )
    results = search_response["result"]["results"]
    insights = [
        {
            "title": (item.get("metadata") or {}).get("raw_title") or item.get("memory", "")[:80],
            "source_id": (item.get("metadata") or {}).get("source_id"),
        }
        for item in results
    ]
    response = {"insights": insights}
    if search_response.get("fallback_grace"):
        response["fallback_grace"] = search_response["fallback_grace"]
    return response


def _mem0_match_search(request: BhmMatchSearchRequest) -> dict:
    search_response = mem0_search(
        SearchRequest(
            query=request.query,
            project=request.project,
            user_id=settings.mem0_user_id,
            top_k=request.limit,
            domain=request.domain,
            semantic_type=request.semantic_type,
            priority=request.priority,
            include_archived=request.include_archived,
            include_logs=request.include_logs,
        )
    )
    result = search_response["result"]["results"]

    mapped = []
    for item in result:
        metadata = item.get("metadata") or {}
        mapped.append(
            {
                "title": metadata.get("raw_title") or item.get("memory", "")[:80],
                "obsId": metadata.get("source_id") or item.get("id"),
                "source_id": metadata.get("source_id") or item.get("id"),
                "content": item.get("memory"),
                "project": metadata.get("project"),
                "type": metadata.get("memory_type"),
                "tags": metadata.get("tags") or [],
            }
        )
    response = {
        "results": mapped,
        "matches": mapped,
        "lessons": [],
        "filters": {
            "project": request.project,
            "domain": request.domain,
            "semantic_type": request.semantic_type,
            "priority": request.priority,
            "include_archived": request.include_archived,
            "include_logs": request.include_logs,
        },
    }
    if search_response.get("fallback_grace"):
        response["fallback_grace"] = search_response["fallback_grace"]
    return response


@app.post("/bhm/remember")
async def bhm_remember(request: RememberRequest) -> dict:
    record = await _run_bounded_write("bhm.remember", _remember_live_memory, request)
    return {
        "success": True,
        "memory": {
            "id": record["source_id"],
            "title": record["metadata"]["raw_title"],
            "project": record["project"],
            "type": record["memory_type"],
            "createdAt": record["created_at"],
        },
    }


@app.post("/bhm/lessons")
async def bhm_lessons_create(request: LessonRequest) -> dict:
    def write_lesson() -> dict:
        lesson_id = f"lesson_bhm_{uuid.uuid4().hex[:16]}"
        item = {
            "id": lesson_id,
            "content": request.content,
            "context": request.context,
            "confidence": request.confidence,
            "project": request.project,
            "tags": request.tags,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        lessons = _load_lessons()
        lessons.append(item)
        _save_lessons(lessons)
        return {"action": "created", "lesson": item}

    return await _run_bounded_write("bhm.lessons", write_lesson)


@app.post("/bhm/lessons/search")
def bhm_lessons_search(request: BhmMatchSearchRequest) -> dict:
    lessons = [item for item in _load_lessons() if not request.project or item.get("project") == request.project]
    query = request.query.lower()
    ranked = []
    for item in lessons:
        text = f"{item.get('content', '')} {' '.join(item.get('tags') or [])}".lower()
        score = 1 if query in text else 0
        if score > 0:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return {"lessons": [item for _, item in ranked[: request.limit]]}


@app.post("/bhm/observe")
async def bhm_observe(request: ObservationIngressV1) -> dict:
    secured_request = _secure_observation_request_model(
        request,
        max_input_bytes=OBSERVATION_MAX_INPUT_BYTES,
    )

    def write_observation() -> dict:
        item = build_observation_record(secured_request)
        _append_observation(item)
        return {"success": True, "observation": item}

    return await _run_bounded_write("bhm.observe", write_observation)


@app.get("/bhm/observations/store/status")
def bhm_observation_store_status(integrity: bool = False) -> dict:
    store = _observation_store()
    return store.status(integrity_check=integrity)


@app.get("/bhm/retention/status")
def bhm_retention_status(project: str | None = None, as_of: str | None = None) -> dict:
    try:
        policy = load_retention_policy(_retention_policy_path())
        effective_as_of = parse_timestamp(as_of) if as_of else datetime.now(timezone.utc)
        if effective_as_of is None:
            raise HTTPException(status_code=422, detail={"error": "invalid_retention_as_of"})
        plan = build_retention_plan(
            _observation_store().retention_candidates(project=project),
            _hook_queue().retention_candidates(project=project),
            policy,
            as_of=effective_as_of,
        )
        return {
            "success": True,
            "mode": "dry-run",
            "project": project or "",
            "plan": summarize_retention_plan(plan),
            "stores": {
                "observations": _observation_store().status(),
                "hookQueue": _hook_queue().status(),
            },
        }
    except HTTPException:
        raise
    except (OSError, RetentionPolicyError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "retention_policy_unavailable", "detail": str(exc)},
        ) from exc


@app.get("/bhm/slots")
def bhm_slots(project: str = "e-github-workspace") -> dict:
    slots = [item for item in _load_slots() if item.get("project") == project]
    return {"slots": slots}


@app.get("/bhm/slot")
def bhm_slot(project: str = "e-github-workspace", label: str = "") -> dict:
    item = _resolve_slot(project, label)
    return {"slot": item}


@app.post("/bhm/slot")
def bhm_slot_create(request: SlotRequest, project: str = "e-github-workspace") -> dict:
    project = request.project or project
    slots = _load_slots()
    slots = [
        item for item in slots
        if not (item.get("project") == project and item.get("label") == request.label)
    ]
    slot = {
        "project": project,
        "label": request.label,
        "content": request.content[: request.sizeLimit],
        "sizeLimit": request.sizeLimit,
        "description": request.description,
        "pinned": request.pinned,
        "scope": request.scope,
    }
    slots.append(slot)
    _save_slots(slots)
    return {"success": True, "slot": slot}


@app.post("/bhm/slot/append")
def bhm_slot_append(request: SlotAppendRequest, project: str = "e-github-workspace") -> dict:
    project = request.project or project
    slots = _load_slots()
    updated = None
    for item in slots:
        if item.get("project") == project and item.get("label") == request.label:
            combined = ((item.get("content") or "") + request.text)[: int(item.get("sizeLimit") or 2000)]
            item["content"] = combined
            updated = item
            break
    if updated is None:
        updated = {
            "project": project,
            "label": request.label,
            "content": request.text[:2000],
            "sizeLimit": 2000,
            "description": "",
            "pinned": True,
            "scope": "project",
        }
        slots.append(updated)
    _save_slots(slots)
    return {"success": True, "slot": updated}


@app.post("/bhm/slot/replace")
def bhm_slot_replace(request: SlotReplaceRequest, project: str = "e-github-workspace") -> dict:
    project = request.project or project
    slots = _load_slots()
    updated = None
    for item in slots:
        if item.get("project") == project and item.get("label") == request.label:
            item["content"] = request.content[: int(item.get("sizeLimit") or 2000)]
            updated = item
            break
    if updated is None:
        updated = {
            "project": project,
            "label": request.label,
            "content": request.content[:2000],
            "sizeLimit": 2000,
            "description": "",
            "pinned": True,
            "scope": "project",
        }
        slots.append(updated)
    _save_slots(slots)
    return {"success": True, "slot": updated}


@app.delete("/bhm/slot")
def bhm_slot_delete(project: str = "e-github-workspace", label: str = "") -> dict:
    slots = _load_slots()
    remaining = [
        item for item in slots
        if not (item.get("project") == project and item.get("label") == label)
    ]
    _save_slots(remaining)
    return {"success": True, "deleted": len(remaining) != len(slots)}


@app.post("/bhm/slot/reflect")
def bhm_slot_reflect(request: SlotLabelRequest, project: str = "e-github-workspace") -> dict:
    project = request.project or project
    slot = _resolve_slot(project, request.label)
    if slot is None:
        raise HTTPException(status_code=404, detail="slot not found")
    return {"success": True, "slot": slot, "reflection": slot.get("content", "")[:200]}


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
