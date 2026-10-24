"""SQLite store base: schema initialization, migrations, and core job and snapshot persistence."""

from __future__ import annotations

from .repository_index_store_schema import _RepositoryIndexStoreSchemaMixin
from .repository_index_store_jobs import _RepositoryIndexStoreJobsMixin
from .repository_index_store_snapshots import _RepositoryIndexStoreSnapshotsMixin
from .repository_index_types import (  # noqa: F401
    RepositoryIndexError,
    RepositoryIndexMigrationRequired,
    RepositorySkip,
    RepositorySourceProvenance,
    RepositoryState,
    REPOSITORY_INDEX_BUSY_TIMEOUT_MS,
    REPOSITORY_INDEX_SCHEMA_VERSION,
    REPOSITORY_INDEX_STORE_SCHEMA_VERSION,
    REPOSITORY_INDEX_WRITE_RETRY_DELAYS,
    _REPOSITORY_INDEX_TABLES,
    _canonical_json,
    _clip,
    _sha256_file,
    _sha256_json,
    _utc_now,
    repository_index_error_retryable,
)

class _RepositoryIndexStoreBase(
    _RepositoryIndexStoreSchemaMixin,
    _RepositoryIndexStoreJobsMixin,
    _RepositoryIndexStoreSnapshotsMixin,
):
    """Repository snapshot tables inside the canonical BHM SQLite database."""
    pass

RepositoryIndexStoreBase = _RepositoryIndexStoreBase
