from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel, Field

from . import memory_contracts as _memory_contracts
from .capability import MCP_OPERATOR_HEADER_STATE
from .config import settings
from .ingress_admission import IngressKind, admit_ingress
from .local_endpoint_policy import MAX_RESPONSE_BYTES, validate_local_endpoint
from .resource_limits import (
    BHM_CODE_GRAPH_HTTP_TIMEOUT_SECONDS,
    BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS,
    BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS,
    BHM_INTERNAL_HTTP_TIMEOUT_SECONDS,
)

MemoryMetadata = _memory_contracts.MemoryMetadata
MemoryClass = _memory_contracts.MemoryClass
MemoryEventRole = _memory_contracts.MemoryEventRole
MetadataActionability = _memory_contracts.MetadataActionability
MetadataDomain = _memory_contracts.MetadataDomain
MetadataLanguage = _memory_contracts.MetadataLanguage
MetadataLifecycle = _memory_contracts.MetadataLifecycle
MetadataPriority = _memory_contracts.MetadataPriority
MetadataProvenance = _memory_contracts.MetadataProvenance
MetadataRetention = _memory_contracts.MetadataRetention
MetadataScope = _memory_contracts.MetadataScope
MetadataSemanticType = _memory_contracts.MetadataSemanticType
MetadataSensitivity = _memory_contracts.MetadataSensitivity
MetadataStakeholder = _memory_contracts.MetadataStakeholder
MetadataVerification = _memory_contracts.MetadataVerification

DEFAULT_PROJECT = "e-github-workspace"
DEFAULT_BASE_URL = os.getenv("BHM_MCP_BASE_URL", f"http://{settings.host}:{settings.port}")
TAXONOMY_METADATA_HINT = (
    "Typed metadata taxonomy values: lifecycle=draft|validated|deprecated|archived; "
    "provenance=github|mcp|llm|human|synthetic; priority=critical|high|medium|low|normal|trivial; "
    "domain=frontend|backend|infra|security|product|general; sensitivity=public|internal|restricted; "
    "scope=global|service|feature|local; retention=transient|short-term|long-term|permanent; "
    "verification=unverified|peer-reviewed|trusted; actionability=task|info|decision|query; "
    "stakeholder=core-team|devops|frontend-squad|product-owner; language=en|ru|code-python|code-ts; "
    "semantic_type=architecture|bugfix|feature|refactor|knowledge|fact|log|error|decision-log|requirement; "
    "memory_class=episodic|semantic|procedural|working|unclassified; "
    "event_role=fact|decision|qa|trace|feedback|skill_run|unclassified; "
    "version=string; importance_score=1..10."
)

mcp = FastMCP(
    "bhm",
    instructions=(
        "BHM is the primary workspace memory system. For a complex task, call bhm_agent_playbook first to obtain read-only, task-specific operating guidance. "
        "Use these tools for memory search, non-authoritative admission, preflight, slots, lessons, "
        "and diagnostics against the local BlackHoleMemory runtime. "
        "Prefer stable public tools for normal agent work. "
        "Treat deprecated compatibility candidates as transitional helpers and prefer their newer replacements."
    ),
)


def _mcp_authority_rejection(*, project: str | None, payload: Any) -> dict[str, Any]:
    """Return a local fail-closed receipt without forwarding LLM tool content."""

    admission = admit_ingress(
        ingress=IngressKind.MCP,
        project=str(project or DEFAULT_PROJECT),
        actor="mcp-tool",
        payload=payload,
    )
    receipt: dict[str, Any] = {"success": False, "code": "ingress_admission_rejected", "admission": admission.receipt()}
    header_state = MCP_OPERATOR_HEADER_STATE.get()
    if header_state:
        receipt["operator_header"] = header_state
    return receipt


class BhmBatchUpsertItem(BaseModel):
    upsert_key: str
    content: str
    project: str | None = None
    memory_type: str = "workflow"
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
    concepts: list[str] | None = None
    files: list[str] | None = None
    metadata: MemoryMetadata | None = None


class BhmBatchLinkItem(BaseModel):
    source_id: str
    target_id: str
    relation: str
    project: str
    metadata: MemoryMetadata | None = None
    ontology_schema_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


def _read_process_or_user_env_value(key: str) -> str | None:
    direct = str(os.getenv(key) or "").strip()
    if direct:
        return direct
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, key)
    except (ImportError, FileNotFoundError, OSError):
        return None
    return str(value or "").strip() or None


def _client(*, timeout: float | None = None) -> httpx.Client:
    # The MCP compatibility bridge is local-only. Validate the configured
    # origin before constructing an authenticated client so a malformed or
    # remote BHM_MCP_BASE_URL can never receive the caller bearer token.
    base_url = validate_local_endpoint(DEFAULT_BASE_URL)
    caller_token = _read_process_or_user_env_value("BHM_CALLER_TOKEN")
    if not caller_token:
        raise RuntimeError("BHM caller credential is unavailable; initialize BHM_CALLER_TOKEN before using MCP")
    headers = {
        "Authorization": f"Bearer {caller_token}",
        "X-BHM-Caller-Surface": "mcp",
    }
    # Admin-surface tools remain capability-gated. Supplying the local
    # operator capability here lets an explicitly configured MCP client call
    # those tools without making the routine core catalog privileged.
    admin_capability = _read_process_or_user_env_value("BHM_ADMIN_CAPABILITY") or _read_process_or_user_env_value(
        "BHM_MCP_ADMIN_CAPABILITY"
    )
    if admin_capability:
        headers["x-bhm-admin-capability"] = admin_capability
    return httpx.Client(
        base_url=base_url,
        timeout=float(BHM_INTERNAL_HTTP_TIMEOUT_SECONDS if timeout is None else timeout),
        headers=headers,
        follow_redirects=False,
        trust_env=False,
    )


def _bounded_json_response(response: httpx.Response) -> Any:
    """Reject oversized MCP→REST responses before JSON parsing."""

    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ValueError("MCP REST response has invalid content-length") from exc
        if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
            raise ValueError("MCP REST response exceeded bounded limit")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("MCP REST response exceeded bounded limit")
    return response.json()


def _read_native_env_value(key: str) -> str | None:
    direct = _read_process_or_user_env_value(key)
    if direct:
        return direct
    env_path = Path.home() / ".bhm" / ".env"
    if not env_path.exists():
        return None
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            current_key, current_value = line.split("=", 1)
            if current_key.strip() == key:
                return current_value.split("#", 1)[0].strip() or None
    except OSError:
        return None
    return None


def _parse_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip() for item in value.split(",")]
    items = [item for item in items if item]
    return items or None


def _jsonable_or_text(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("metadata_json must be a JSON object")
    return parsed


def _metadata_payload(value: MemoryMetadata | dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        return value.dict(exclude_none=True)
    return MemoryMetadata(**value).model_dump(mode="json", exclude_none=True)


def _metadata_json_object(value: str | None) -> dict[str, Any] | None:
    return _metadata_payload(_json_object(value))


def _model_dump(value: BaseModel) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value.dict(exclude_none=True)


def _batch_upsert_item_payload(item: BhmBatchUpsertItem) -> dict[str, Any]:
    payload = _model_dump(item)
    payload["type"] = payload.pop("memory_type", "workflow")
    return payload


def _batch_link_item_payload(item: BhmBatchLinkItem) -> dict[str, Any]:
    return _model_dump(item)


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as client:
        response = client.get(path, params=params)
        response.raise_for_status()
        return _bounded_json_response(response)


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    timeout: float | None = None
    if path == "/bhm/code-tools":
        operation = str(body.get("operation") or "").casefold()
        if operation in {"status", "coverage", "projects"}:
            timeout = float(BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS)
        elif operation in {"index", "watch"}:
            timeout = float(
                BHM_CODE_GRAPH_HTTP_TIMEOUT_SECONDS
                if body.get("graph_only") or (body.get("build_graph") and not body.get("defer_graph"))
                else BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS
            )
    with _client(timeout=timeout) as client:
        response = client.post(path, json=body)
        response.raise_for_status()
        return _bounded_json_response(response)


def _delete(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as client:
        response = client.delete(path, params=params)
        response.raise_for_status()
        return _bounded_json_response(response)


def _delete_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    with _client() as client:
        response = client.request("DELETE", path, json=body)
        response.raise_for_status()
        return _bounded_json_response(response)

