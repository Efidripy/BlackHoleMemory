from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


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
