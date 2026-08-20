import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .qdrant_runtime import QDRANT_DEFAULT_URL
from .runtime_endpoints import endpoint_host
from .runtime_endpoints import endpoint_port
from .runtime_endpoints import endpoint_url


def _memory_env_candidates() -> list[Path]:
    return [
        Path.home() / ".bhm" / ".env",
    ]


def _read_memory_env_value(key: str, default: str = "") -> str:
    candidate = os.getenv(key)
    if candidate:
        return candidate

    for env_path in _memory_env_candidates():
        if not env_path.exists():
            continue

        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            current_key, current_value = line.split("=", 1)
            if current_key.strip() == key:
                value = current_value.split("#", 1)[0].strip()
                return value or default

    return default


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BHM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "BlackHoleMemory"
    app_env: str = "dev"
    host: str = endpoint_host("bhm_api")
    port: int = endpoint_port("bhm_api")
    context_profile: str = "standard"

    qdrant_url: str = QDRANT_DEFAULT_URL
    qdrant_collection: str = "blackholememory"
    qdrant_api_key: str = ""

    mem0_enabled: bool = True
    mem0_provider_hint: str = "python-package"
    mem0_collection_name: str = "blackholememory-mem0"
    mem0_user_id: str = "blackholememory-import"
    mem0_embedding_dims: int = 768
    mem0_llm_provider: str = "openai"
    mem0_embedder_provider: str = "openai"
    mem0_api_key: str = _read_memory_env_value("OPENAI_API_KEY", "")
    mem0_openai_base_url: str = _read_memory_env_value("OPENAI_BASE_URL", endpoint_url("llm_default"))
    mem0_llm_model: str = _read_memory_env_value("OPENAI_MODEL", "qwen2.5-coder-7b-instruct")
    mem0_embedding_model: str = _read_memory_env_value(
        "BHM_MEM0_EMBEDDING_MODEL",
        "text-embedding-nomic-embed-text-v1.5",
    )
    repo_root: Path = Path(__file__).resolve().parents[2]
    runtime_dir: Path = repo_root / ".runtime"

    sqlite_retention_enabled: bool = True
    sqlite_retention_initial_delay_seconds: float = 300.0
    sqlite_retention_interval_seconds: float = 21_600.0
    sqlite_retention_keep_graph_history: int = 2
    sqlite_retention_keep_index_history: int = 2
    sqlite_retention_keep_completed_outbox: int = 1_000
    sqlite_retention_keep_latest_outbox_per_aggregate: int = 1
    sqlite_retention_graph_min_age_days: int = 7
    sqlite_retention_index_min_age_days: int = 7
    sqlite_retention_outbox_min_age_days: int = 30
    sqlite_retention_max_graph_snapshots_per_cycle: int = 2
    sqlite_retention_max_index_snapshots_per_cycle: int = 2
    sqlite_retention_max_outbox_events_per_cycle: int = 250


settings = Settings()
