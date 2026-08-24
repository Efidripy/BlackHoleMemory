"""Versioned local-only LLM gateway contracts and OpenAI-compatible adapter."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from .llm_safety import PROPOSAL_AUTHORITY
from .llm_safety import sanitize_llm_messages
from .llm_safety import sanitize_llm_value
from .llm_telemetry import LLMTelemetry
from .llm_telemetry import get_llm_telemetry
from .local_endpoint_policy import MAX_RESPONSE_BYTES
from .local_endpoint_policy import LocalEndpointError
from .local_endpoint_policy import open_local_url
from .local_endpoint_policy import read_bounded_response
from .local_endpoint_policy import validate_local_endpoint
from .resource_limits import LLM_HTTP_TIMEOUT_SECONDS


GATEWAY_SCHEMA_VERSION = "bhm.llm.gateway.v1"
MAX_RESPONSE_CHARS = 32_000
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class PromptDefinition:
    prompt_id: str
    version: str
    system: str
    output_mode: str = "text"


@dataclass(frozen=True)
class ModelDefinition:
    model_id: str
    base_url: str
    capabilities: frozenset[str] = frozenset()
    local_only: bool = True
    api_key: str = ""


@dataclass(frozen=True)
class GatewayRequest:
    request_id: str
    prompt_id: str
    model_id: str
    messages: tuple[dict[str, Any], ...]
    max_tokens: int = 256
    temperature: float = 0.0
    json_required_keys: tuple[str, ...] = ()
    json_schema: dict[str, Any] | None = None
    timeout_seconds: float = 30.0
    tools: tuple[dict[str, Any], ...] = ()
    tool_choice: str | dict[str, Any] | None = None
    project: str = "blackholememory"
    source: str = "local-llm-gateway"
    workload: str = "foreground"
    queue_wait_ms: float = 0.0
    # A deliberately narrow compatibility hint for local chat templates.  Do
    # not turn this into an arbitrary provider-extra-body escape hatch: the
    # gateway remains the policy boundary for every outbound local request.
    chat_template_kwargs: dict[str, bool] | None = None


@dataclass
class GatewayResult:
    request_id: str
    model_id: str
    ok: bool
    content: str = ""
    parsed_json: dict[str, Any] | None = None
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    message: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    authority: str = PROPOSAL_AUTHORITY
    auto_apply: bool = False
    requires_validation: bool = True
    failure: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GATEWAY_SCHEMA_VERSION,
            "request_id": self.request_id,
            "model_id": self.model_id,
            "ok": self.ok,
            "content": self.content,
            "parsed_json": self.parsed_json,
            "latency_ms": round(self.latency_ms, 3),
            "usage": self.usage,
            "validation": self.validation,
            "message": self.message,
            "provenance": self.provenance,
            "authority": self.authority,
            "auto_apply": self.auto_apply,
            "requires_validation": self.requires_validation,
            "failure": self.failure,
        }


class PromptRegistry:
    def __init__(self, definitions: list[PromptDefinition] | None = None) -> None:
        self._definitions: dict[str, PromptDefinition] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: PromptDefinition) -> None:
        if not definition.prompt_id.strip() or not definition.version.strip():
            raise ValueError("prompt id and version are required")
        self._definitions[definition.prompt_id] = definition

    def get(self, prompt_id: str) -> PromptDefinition:
        try:
            return self._definitions[str(prompt_id).strip()]
        except KeyError as exc:
            raise ValueError(f"unknown prompt: {prompt_id!r}") from exc

    def snapshot(self) -> list[dict[str, str]]:
        return [
            {"prompt_id": item.prompt_id, "version": item.version, "output_mode": item.output_mode}
            for item in sorted(self._definitions.values(), key=lambda value: value.prompt_id)
        ]


class ModelRegistry:
    def __init__(self, definitions: list[ModelDefinition] | None = None) -> None:
        self._definitions: dict[str, ModelDefinition] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: ModelDefinition) -> None:
        if not definition.model_id.strip():
            raise ValueError("model id is required")
        if definition.local_only and not is_local_endpoint(definition.base_url):
            raise ValueError("local-only model must use a loopback/private endpoint")
        self._definitions[definition.model_id] = definition

    def get(self, model_id: str) -> ModelDefinition:
        try:
            return self._definitions[str(model_id).strip()]
        except KeyError as exc:
            raise ValueError(f"unknown model: {model_id!r}") from exc

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "model_id": item.model_id,
                "base_url": item.base_url,
                "capabilities": sorted(item.capabilities),
                "local_only": item.local_only,
                "api_key_present": bool(item.api_key),
            }
            for item in sorted(self._definitions.values(), key=lambda value: value.model_id)
        ]


Transport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]
AsyncTransport = Callable[[str, dict[str, Any], dict[str, str], float], Awaitable[dict[str, Any]]]


def _bounded_request_timeout(value: float) -> float:
    """Return a finite gateway timeout within the registry-backed envelope."""

    try:
        requested = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("LLM request timeout must be numeric") from exc
    if not math.isfinite(requested):
        raise ValueError("LLM request timeout must be finite")
    return max(min(requested, float(LLM_HTTP_TIMEOUT_SECONDS)), 1.0)


def _bounded_chat_template_kwargs(value: dict[str, bool] | None) -> dict[str, bool]:
    """Validate the small local-template compatibility contract.

    LM Studio's Qwen template accepts ``enable_thinking``.  Keeping this
    allowlist explicit prevents callers from injecting arbitrary provider
    request fields through an otherwise generic gateway request.
    """

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("chat template kwargs must be a mapping")
    if set(value) - {"enable_thinking"}:
        raise ValueError("unsupported chat template kwargs")
    enable_thinking = value.get("enable_thinking")
    if not isinstance(enable_thinking, bool):
        raise ValueError("enable_thinking must be a boolean")
    return {"enable_thinking": enable_thinking}


class LocalOpenAICompatibleAdapter:
    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = transport or _http_transport

    def complete(self, request: GatewayRequest, *, prompt: PromptDefinition, model: ModelDefinition) -> GatewayResult:
        if model.local_only and not is_local_endpoint(model.base_url):
            return GatewayResult(
                request_id=request.request_id,
                model_id=model.model_id,
                ok=False,
                failure={"code": "non_local_endpoint", "message": "local-only model rejected non-local endpoint"},
            )
        started = time.perf_counter()
        payload = self._payload(request, prompt, model)
        headers = self._headers(model)
        try:
            response = self._transport(
                model.base_url.rstrip("/") + "/chat/completions",
                payload,
                headers,
                _bounded_request_timeout(request.timeout_seconds),
            )
        except Exception as exc:  # adapter boundary returns structured failure
            return GatewayResult(
                request_id=request.request_id,
                model_id=model.model_id,
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                failure={"code": "transport_error", "message": str(exc)[:240]},
            )
        return self._result(request, model, response, started, prompt)

    async def acomplete(
        self,
        request: GatewayRequest,
        *,
        prompt: PromptDefinition,
        model: ModelDefinition,
        transport: AsyncTransport | None = None,
    ) -> GatewayResult:
        if transport is None:
            return await asyncio.to_thread(self.complete, request, prompt=prompt, model=model)
        if model.local_only and not is_local_endpoint(model.base_url):
            return GatewayResult(
                request_id=request.request_id,
                model_id=model.model_id,
                ok=False,
                failure={"code": "non_local_endpoint", "message": "local-only model rejected non-local endpoint"},
            )
        started = time.perf_counter()
        payload = self._payload(request, prompt, model)
        headers = self._headers(model)
        try:
            response = await transport(
                model.base_url.rstrip("/") + "/chat/completions",
                payload,
                headers,
                _bounded_request_timeout(request.timeout_seconds),
            )
        except Exception as exc:
            return GatewayResult(
                request_id=request.request_id,
                model_id=model.model_id,
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                failure={"code": "transport_error", "message": str(exc)[:240]},
            )
        return self._result(request, model, response, started, prompt)

    @staticmethod
    def _payload(request: GatewayRequest, prompt: PromptDefinition, model: ModelDefinition) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model.model_id,
            "messages": ([{"role": "system", "content": prompt.system}] if prompt.system else [])
            + list(request.messages),
            "temperature": max(min(float(request.temperature), 2.0), 0.0),
            "max_tokens": max(min(int(request.max_tokens), 4096), 1),
        }
        if request.json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": request.json_schema,
            }
        elif prompt.output_mode == "json" or request.json_required_keys:
            # LM Studio's OpenAI-compatible endpoint rejects the legacy
            # ``json_object`` discriminator (it accepts ``text`` or
            # ``json_schema``).  The gateway still enforces the required JSON
            # keys deterministically after parsing, so ``text`` preserves the
            # safety contract without relying on provider-specific JSON mode.
            payload["response_format"] = {"type": "text"}
        if request.tools:
            payload["tools"] = list(request.tools)
            payload["tool_choice"] = request.tool_choice or "auto"
        chat_template_kwargs = _bounded_chat_template_kwargs(request.chat_template_kwargs)
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs
        return payload

    @staticmethod
    def _headers(model: ModelDefinition) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if model.api_key:
            headers["Authorization"] = f"Bearer {model.api_key}"
        return headers

    @staticmethod
    def _result(
        request: GatewayRequest,
        model: ModelDefinition,
        response: dict[str, Any],
        started: float,
        prompt: PromptDefinition,
    ) -> GatewayResult:
        message = _message_from_response(response)
        content = _content_from_message(message)
        expects_json = prompt.output_mode == "json" or bool(request.json_required_keys)
        parsed = normalize_json_content(content) if expects_json else None
        validation = validate_json_payload(parsed, request.json_required_keys) if expects_json else {"checked": False}
        has_tool_calls = bool(message.get("tool_calls"))
        ok = bool(content or has_tool_calls) and (not request.json_required_keys or validation["ok"])
        return GatewayResult(
            request_id=request.request_id,
            model_id=model.model_id,
            ok=ok,
            content=content[:MAX_RESPONSE_CHARS],
            parsed_json=parsed,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            usage=response.get("usage") if isinstance(response.get("usage"), dict) else {},
            validation=validation,
            message=message,
            failure=None if ok else {"code": "schema_validation_failed", "message": "response did not satisfy gateway schema"},
        )


class LocalLLMGateway:
    def __init__(
        self,
        *,
        prompts: PromptRegistry,
        models: ModelRegistry,
        adapter: LocalOpenAICompatibleAdapter,
        telemetry: LLMTelemetry | None = None,
    ) -> None:
        self.prompts = prompts
        self.models = models
        self.adapter = adapter
        self.telemetry = telemetry or get_llm_telemetry()

    def complete(self, request: GatewayRequest) -> GatewayResult:
        prompt = self.prompts.get(request.prompt_id)
        model = self.models.get(request.model_id)
        safe_request, safe_prompt, provenance = _prepare_safe_request(request, prompt)
        result = self.adapter.complete(safe_request, prompt=safe_prompt, model=model)
        result = _apply_result_safety(result, request=safe_request, provenance=provenance)
        self.telemetry.record_gateway_result(
            result,
            job_type=request.prompt_id,
            workload=request.workload,
            project=request.project,
            queue_wait_ms=request.queue_wait_ms,
        )
        return result

    async def acomplete(self, request: GatewayRequest) -> GatewayResult:
        """Run the blocking adapter without blocking an async caller."""

        prompt = self.prompts.get(request.prompt_id)
        model = self.models.get(request.model_id)
        safe_request, safe_prompt, provenance = _prepare_safe_request(request, prompt)
        result = await self.adapter.acomplete(safe_request, prompt=safe_prompt, model=model)
        result = _apply_result_safety(result, request=safe_request, provenance=provenance)
        self.telemetry.record_gateway_result(
            result,
            job_type=request.prompt_id,
            workload=request.workload,
            project=request.project,
            queue_wait_ms=request.queue_wait_ms,
        )
        return result

    async def acomplete_with_transport(self, request: GatewayRequest, transport: AsyncTransport) -> GatewayResult:
        prompt = self.prompts.get(request.prompt_id)
        model = self.models.get(request.model_id)
        safe_request, safe_prompt, provenance = _prepare_safe_request(request, prompt)
        result = await self.adapter.acomplete(safe_request, prompt=safe_prompt, model=model, transport=transport)
        result = _apply_result_safety(result, request=safe_request, provenance=provenance)
        self.telemetry.record_gateway_result(
            result,
            job_type=request.prompt_id,
            workload=request.workload,
            project=request.project,
            queue_wait_ms=request.queue_wait_ms,
        )
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": GATEWAY_SCHEMA_VERSION,
            "prompts": self.prompts.snapshot(),
            "models": self.models.snapshot(),
            "local_only": all(item["local_only"] for item in self.models.snapshot()),
            "telemetry": self.telemetry.snapshot(),
        }


def normalize_json_content(content: str) -> dict[str, Any] | None:
    cleaned = str(content or "").strip()
    if cleaned.startswith("```"):
        cleaned = _FENCE_RE.sub("", cleaned).strip()
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def validate_json_payload(payload: dict[str, Any] | None, required_keys: tuple[str, ...]) -> dict[str, Any]:
    missing = [key for key in required_keys if payload is None or key not in payload]
    return {"checked": True, "ok": not missing, "missing_keys": missing}


def is_local_endpoint(base_url: str) -> bool:
    try:
        validate_local_endpoint(base_url)
    except (LocalEndpointError, ValueError):
        return False
    return True


def _http_transport(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with open_local_url(request, timeout=timeout) as response:
            value = json.loads(read_bounded_response(response, limit=MAX_RESPONSE_BYTES).decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(str(exc)) from exc
    if not isinstance(value, dict):
        raise RuntimeError("gateway adapter expected JSON object")
    return value


def _message_from_response(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") if isinstance(response.get("choices"), list) else []
    if not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
    if message:
        return dict(message)
    text = choices[0].get("text")
    return {"content": str(text or "")} if text is not None else {}


def _content_from_message(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return str(content or "")


def _prepare_safe_request(
    request: GatewayRequest,
    prompt: PromptDefinition,
) -> tuple[GatewayRequest, PromptDefinition, dict[str, Any]]:
    message_transform = sanitize_llm_messages(
        request.messages,
        source=request.source,
        project=request.project,
    )
    prompt_transform = sanitize_llm_value(
        prompt.system,
        source=f"{request.source}:prompt",
        project=request.project,
    )
    provenance = dict(message_transform.provenance)
    provenance["prompt"] = prompt_transform.provenance
    safe_request = replace(request, messages=tuple(message_transform.value))
    safe_prompt = replace(prompt, system=str(prompt_transform.value or ""))
    return safe_request, safe_prompt, provenance


def _apply_result_safety(
    result: GatewayResult,
    *,
    request: GatewayRequest,
    provenance: dict[str, Any],
) -> GatewayResult:
    content_transform = sanitize_llm_value(
        result.content,
        source="local-llm-output",
        project=request.project,
    )
    message_transform = sanitize_llm_value(
        result.message,
        source="local-llm-output-message",
        project=request.project,
    )
    result.content = str(content_transform.value or "")[:MAX_RESPONSE_CHARS]
    result.message = message_transform.value if isinstance(message_transform.value, dict) else {}
    result.provenance = {
        **provenance,
        "output": content_transform.provenance,
        "output_message": message_transform.provenance,
        "authority": PROPOSAL_AUTHORITY,
        "auto_apply": False,
        "requires_validation": True,
    }
    result.authority = PROPOSAL_AUTHORITY
    result.auto_apply = False
    result.requires_validation = True
    if request.json_required_keys and (
        result.failure is None or result.failure.get("code") == "schema_validation_failed"
    ):
        result.parsed_json = normalize_json_content(result.content)
        result.validation = validate_json_payload(result.parsed_json, request.json_required_keys)
        if not result.validation["ok"]:
            result.ok = False
            result.failure = {
                "code": "schema_validation_failed",
                "message": "sanitized response did not satisfy gateway schema",
            }
    return result


__all__ = [
    "GATEWAY_SCHEMA_VERSION",
    "GatewayRequest",
    "GatewayResult",
    "LocalLLMGateway",
    "LocalOpenAICompatibleAdapter",
    "ModelDefinition",
    "ModelRegistry",
    "PromptDefinition",
    "PromptRegistry",
    "is_local_endpoint",
    "normalize_json_content",
    "validate_json_payload",
]
