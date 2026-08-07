"""Shared REST/MCP memory metadata contract.

This module is intentionally transport-neutral.  REST and MCP adapters import
the same Pydantic model so schema drift cannot silently reappear between the
two surfaces.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


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


__all__ = [
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
]
