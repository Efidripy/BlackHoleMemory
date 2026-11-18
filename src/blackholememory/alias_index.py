"""Optional, revision-bound access index for generated questions."""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from .filesystem_boundaries import assert_safe_path

ALIAS_RETRIEVAL_ENV = "BHM_ALIAS_RETRIEVAL"
ALIAS_INDEX_CAPABILITY_KEY = "alias_index_schema"
ALIAS_INDEX_CAPABILITY_VERSION = "bhm.alias-index.v1"
ALIAS_TABLE = "memory_aliases"
ALIAS_TOKEN_TABLE = "memory_alias_tokens"
_STOP = frozenset("the and for are was how what why with from this that \u043a\u0430\u043a \u0447\u0442\u043e \u0434\u043b\u044f \u044d\u0442\u043e \u0438\u043b\u0438 \u043f\u0440\u0438 \u0433\u0434\u0435 \u043f\u043e\u0447\u0435\u043c\u0443 \u043a\u043e\u0433\u0434\u0430 \u043a\u0430\u043a\u0438\u0435 \u043a\u0430\u043a\u043e\u0439".split())
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def alias_retrieval_enabled() -> bool:
    return os.getenv(ALIAS_RETRIEVAL_ENV, "").strip().casefold() in {"1", "true", "yes", "on"}


def alias_tokens(value: str) -> tuple[str, ...]:
    words = (word.casefold() for word in _WORD.findall(str(value or "")))
    return tuple(dict.fromkeys(word for word in words if len(word) >= 3 and word not in _STOP))[:16]


def migrate_alias_index(database: str | Path) -> None:
    """Create the derived tables atomically; repeated applications preserve data."""
    path = assert_safe_path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError("memory database is missing")
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS memory_aliases (project TEXT NOT NULL, memory_id TEXT NOT NULL, revision_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY(project, memory_id, revision_id, alias), FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE)")
        connection.execute("CREATE TABLE IF NOT EXISTS memory_alias_tokens (project TEXT NOT NULL, token TEXT NOT NULL, memory_id TEXT NOT NULL, revision_id TEXT NOT NULL, PRIMARY KEY(project, token, memory_id, revision_id), FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_aliases_memory_id ON memory_aliases(memory_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_alias_tokens_memory_id ON memory_alias_tokens(memory_id)")
        connection.execute("INSERT OR IGNORE INTO memory_store_meta(key, value) VALUES (?, ?)", (ALIAS_INDEX_CAPABILITY_KEY, ALIAS_INDEX_CAPABILITY_VERSION))
        marker = connection.execute("SELECT value FROM memory_store_meta WHERE key = ?", (ALIAS_INDEX_CAPABILITY_KEY,)).fetchone()
        if not marker or marker[0] != ALIAS_INDEX_CAPABILITY_VERSION:
            raise ValueError("incompatible alias index capability")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
