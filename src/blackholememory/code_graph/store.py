"""Durable SQLite code graph store."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping

from ..filesystem_boundaries import assert_safe_path
from ..repository_index import SQLiteRepositoryIndexStore
from ._core import (
    CODE_GRAPH_BUSY_TIMEOUT_MS,
    CODE_GRAPH_EXTRACTOR_VERSION,
    CODE_GRAPH_STORE_SCHEMA_VERSION,
    _CODE_GRAPH_TABLES,
    CodeGraphError,
    CodeGraphInjectedFailure,
    _canonical_json,
    _sha256,
    _utc_now,
)


class SQLiteCodeGraphStore:
    """Durable code graph tables in the canonical SQLite database."""

    def __init__(self, database_path: str | Path) -> None:
        # Preserve lexical provenance until shared boundary admission; eager
        # resolution would conceal symlink/junction and hardlink targets.
        self.path = Path(database_path).expanduser()
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        assert_safe_path(self.path)
        if read_only:
            connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=CODE_GRAPH_BUSY_TIMEOUT_MS / 1000)
        else:
            connection = sqlite3.connect(self.path, timeout=CODE_GRAPH_BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={CODE_GRAPH_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def inspect_schema(self, *, fast: bool = False) -> dict[str, Any]:
        """Inspect graph schema without mutating SQLite.

        Hot read consumers may request ``fast=True`` to validate table shape
        and schema version without scanning every graph table or running the
        full ``PRAGMA quick_check``.  The default remains integrity-complete
        for operator/acceptance diagnostics.
        """

        assert_safe_path(self.path)
        if not self.path.is_file():
            return {"database_exists": False, "schema_version": None, "ready": False, "tables": [], "missing_tables": sorted(_CODE_GRAPH_TABLES), "quick_check": None, "row_counts": {}}
        connection = self._connect(read_only=True)
        try:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            graph_tables = sorted(table for table in tables if table.startswith("repository_code_graph_"))
            version = None
            if "repository_code_graph_meta" in tables:
                row = connection.execute("SELECT value FROM repository_code_graph_meta WHERE key='schema_version'").fetchone()
                version = int(row["value"]) if row else None
            counts = {} if fast else {table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in graph_tables}
            missing = sorted(_CODE_GRAPH_TABLES - tables)
            quick = None if fast else str(connection.execute("PRAGMA quick_check").fetchone()[0])
            return {"database_exists": True, "schema_version": version, "ready": version == CODE_GRAPH_STORE_SCHEMA_VERSION and not missing and (fast or quick == "ok"), "tables": graph_tables, "missing_tables": missing, "quick_check": quick, "row_counts": counts}
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            assert_safe_path(self.path)
            index_status = SQLiteRepositoryIndexStore(self.path).inspect_schema()
            if not index_status.get("ready"):
                raise CodeGraphError("WI-01 repository-index schema must be ready before graph initialization")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            assert_safe_path(self.path.parent, reject_hardlink_target=False)
            assert_safe_path(self.path)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS repository_code_graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS repository_code_graph_snapshots (
                        graph_snapshot_id TEXT PRIMARY KEY,
                        repository_snapshot_id TEXT NOT NULL,
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        root_path TEXT NOT NULL,
                        graph_input_digest TEXT NOT NULL,
                        parser_registry_digest TEXT NOT NULL,
                        graph_digest TEXT NOT NULL UNIQUE,
                        nodes_digest TEXT NOT NULL,
                        edges_digest TEXT NOT NULL,
                        parse_digest TEXT NOT NULL,
                        previous_graph_snapshot_id TEXT,
                        status TEXT NOT NULL CHECK(status IN ('building','completed','failed')),
                        summary_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_code_graph_snapshot_input
                        ON repository_code_graph_snapshots(repository_snapshot_id, parser_registry_digest);
                    CREATE TABLE IF NOT EXISTS repository_code_graph_nodes (
                        graph_snapshot_id TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        stable_key TEXT NOT NULL,
                        node_kind TEXT NOT NULL,
                        path TEXT NOT NULL,
                        name TEXT NOT NULL,
                        qualified_name TEXT NOT NULL,
                        language TEXT NOT NULL,
                        start_line INTEGER,
                        end_line INTEGER,
                        signature TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        parser_version TEXT NOT NULL,
                        provenance_json TEXT NOT NULL,
                        attributes_json TEXT NOT NULL,
                        PRIMARY KEY(graph_snapshot_id, node_id),
                        UNIQUE(graph_snapshot_id, stable_key),
                        FOREIGN KEY(graph_snapshot_id) REFERENCES repository_code_graph_snapshots(graph_snapshot_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_code_graph_nodes_lookup
                        ON repository_code_graph_nodes(graph_snapshot_id, node_kind, path, name);
                    CREATE VIRTUAL TABLE IF NOT EXISTS repository_code_graph_metadata_fts USING fts5(
                        graph_snapshot_id UNINDEXED,
                        node_id UNINDEXED,
                        path,
                        name,
                        qualified_name,
                        signature,
                        language,
                        node_kind
                    );
                    CREATE TABLE IF NOT EXISTS repository_code_graph_edges (
                        graph_snapshot_id TEXT NOT NULL,
                        edge_id TEXT NOT NULL,
                        stable_key TEXT NOT NULL,
                        edge_kind TEXT NOT NULL,
                        source_node_id TEXT NOT NULL,
                        target_node_id TEXT NOT NULL,
                        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                        unresolved INTEGER NOT NULL CHECK(unresolved IN (0,1)),
                        extractor_version TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        attributes_json TEXT NOT NULL,
                        PRIMARY KEY(graph_snapshot_id, edge_id),
                        UNIQUE(graph_snapshot_id, stable_key),
                        FOREIGN KEY(graph_snapshot_id) REFERENCES repository_code_graph_snapshots(graph_snapshot_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_code_graph_edges_kind
                        ON repository_code_graph_edges(graph_snapshot_id, edge_kind, source_node_id, target_node_id);
                    CREATE INDEX IF NOT EXISTS idx_code_graph_edges_source
                        ON repository_code_graph_edges(graph_snapshot_id, source_node_id, stable_key);
                    CREATE INDEX IF NOT EXISTS idx_code_graph_edges_target
                        ON repository_code_graph_edges(graph_snapshot_id, target_node_id, stable_key);
                    CREATE TABLE IF NOT EXISTS repository_code_graph_parse_results (
                        graph_snapshot_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        language TEXT NOT NULL,
                        parser_id TEXT NOT NULL,
                        parser_version TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('parsed','metadata-only','error')),
                        error_code TEXT NOT NULL,
                        error_detail TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        node_count INTEGER NOT NULL,
                        edge_count INTEGER NOT NULL,
                        PRIMARY KEY(graph_snapshot_id, path),
                        FOREIGN KEY(graph_snapshot_id) REFERENCES repository_code_graph_snapshots(graph_snapshot_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS repository_code_graph_current (
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        graph_snapshot_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(project, root_id),
                        FOREIGN KEY(graph_snapshot_id) REFERENCES repository_code_graph_snapshots(graph_snapshot_id)
                    );
                    """
                )
                stored = connection.execute("SELECT value FROM repository_code_graph_meta WHERE key='schema_version'").fetchone()
                if stored is not None and int(stored["value"]) != CODE_GRAPH_STORE_SCHEMA_VERSION:
                    raise CodeGraphError(f"unsupported code graph schema {stored['value']}")
                connection.execute("INSERT OR REPLACE INTO repository_code_graph_meta(key,value) VALUES('schema_version',?)", (str(CODE_GRAPH_STORE_SCHEMA_VERSION),))
                connection.execute("INSERT OR IGNORE INTO repository_code_graph_meta(key,value) VALUES('created_at',?)", (_utc_now(),))
                connection.commit()
                self._initialized = True
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def current_snapshot(self, project: str, root_id: str, *, include_material: bool = False) -> dict[str, Any] | None:
        schema = self.inspect_schema(fast=True)
        if not schema.get("ready"):
            return None
        connection = self._connect(read_only=True)
        try:
            row = connection.execute("SELECT snapshots.* FROM repository_code_graph_current AS current JOIN repository_code_graph_snapshots AS snapshots ON snapshots.graph_snapshot_id=current.graph_snapshot_id WHERE current.project=? AND current.root_id=?", (project, root_id)).fetchone()
            if row is None:
                return None
            return self._snapshot_payload(connection, row, include_material=include_material)
        finally:
            connection.close()

    def snapshot(self, graph_snapshot_id: str, *, include_material: bool = False, read_only: bool = False) -> dict[str, Any]:
        """Return one graph snapshot, optionally without any schema writes.

        Query/explain consumers must use ``read_only=True`` so a read path does
        not run the normal initialization migration or change SQLite journal
        state.  Builders retain the default initializing behavior.
        """
        if read_only:
            schema = self.inspect_schema(fast=True)
            if not schema.get("ready"):
                raise CodeGraphError("code graph schema is not ready")
        else:
            self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute("SELECT * FROM repository_code_graph_snapshots WHERE graph_snapshot_id=?", (graph_snapshot_id,)).fetchone()
            if row is None:
                raise CodeGraphError(f"code graph snapshot not found: {graph_snapshot_id}")
            return self._snapshot_payload(connection, row, include_material=include_material)
        finally:
            connection.close()

    @staticmethod
    def _snapshot_payload(connection: sqlite3.Connection, row: sqlite3.Row, *, include_material: bool) -> dict[str, Any]:
        payload = dict(row)
        payload["summary"] = json.loads(str(payload.pop("summary_json")))
        if include_material:
            payload["nodes"] = []
            for item in connection.execute("SELECT * FROM repository_code_graph_nodes WHERE graph_snapshot_id=? ORDER BY stable_key", (row["graph_snapshot_id"],)).fetchall():
                value = dict(item)
                value["provenance"] = json.loads(value.pop("provenance_json"))
                value["attributes"] = json.loads(value.pop("attributes_json"))
                payload["nodes"].append(value)
            payload["edges"] = []
            for item in connection.execute("SELECT * FROM repository_code_graph_edges WHERE graph_snapshot_id=? ORDER BY stable_key", (row["graph_snapshot_id"],)).fetchall():
                value = dict(item)
                value["unresolved"] = bool(value["unresolved"])
                value["evidence"] = json.loads(value.pop("evidence_json"))
                value["attributes"] = json.loads(value.pop("attributes_json"))
                payload["edges"].append(value)
            payload["parse_results"] = [dict(item) for item in connection.execute("SELECT * FROM repository_code_graph_parse_results WHERE graph_snapshot_id=? ORDER BY path", (row["graph_snapshot_id"],)).fetchall()]
        return payload

    def publish_graph(self, snapshot: Mapping[str, Any], material: Mapping[str, Any], *, fail_before_publish: bool = False) -> dict[str, Any]:
        self.initialize()
        graph_digest = str(material.get("graph_digest") or "")
        if not graph_digest:
            raise CodeGraphError("graph digest is required")
        graph_snapshot_id = f"graph_{_sha256(f'{snapshot.get('snapshot_id')}:{graph_digest}')[:24]}"
        project = str(snapshot.get("project") or "")
        root_id = str(snapshot.get("root_id") or "")
        with self._lock:
            connection = self._connect()
            try:
                existing = connection.execute("SELECT graph_snapshot_id, graph_digest FROM repository_code_graph_snapshots WHERE repository_snapshot_id=? AND parser_registry_digest=?", (snapshot.get("snapshot_id"), material.get("parser_registry_digest"))).fetchone()
                if existing is not None:
                    if str(existing["graph_digest"]) == graph_digest:
                        connection.close()
                        return self.snapshot(str(existing["graph_snapshot_id"]), include_material=False)
                    # The parser registry digest intentionally tracks language
                    # families, while extractor changes (for example bounded
                    # alias metadata or new edge evidence) can alter a graph
                    # without changing that registry. Keep the old immutable
                    # snapshot and publish a new content-addressed snapshot;
                    # current_graph below becomes the authoritative pointer.
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute("SELECT graph_snapshot_id FROM repository_code_graph_current WHERE project=? AND root_id=?", (project, root_id)).fetchone()
                now = _utc_now()
                connection.execute("INSERT INTO repository_code_graph_snapshots(graph_snapshot_id,repository_snapshot_id,project,root_id,root_path,graph_input_digest,parser_registry_digest,graph_digest,nodes_digest,edges_digest,parse_digest,previous_graph_snapshot_id,status,summary_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (graph_snapshot_id, snapshot.get("snapshot_id"), project, root_id, snapshot.get("root_path"), snapshot.get("graph_input_digest"), material.get("parser_registry_digest"), graph_digest, material.get("nodes_digest"), material.get("edges_digest"), material.get("parse_digest"), previous["graph_snapshot_id"] if previous else None, "building", _canonical_json(material.get("summary") or {}), now))
                nodes = list(material.get("nodes") or [])
                edges = list(material.get("edges") or [])
                parses = list(material.get("parse_results") or [])
                connection.executemany("INSERT INTO repository_code_graph_nodes(graph_snapshot_id,node_id,stable_key,node_kind,path,name,qualified_name,language,start_line,end_line,signature,content_sha256,parser_version,provenance_json,attributes_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [(graph_snapshot_id, item["node_id"], item["stable_key"], item["node_kind"], item.get("path") or "", item.get("name") or "", item.get("qualified_name") or "", item.get("language") or "", item.get("start_line"), item.get("end_line"), item.get("signature") or "", item.get("content_sha256") or "", item.get("parser_version") or CODE_GRAPH_EXTRACTOR_VERSION, _canonical_json(item.get("provenance") or {}), _canonical_json(item.get("attributes") or {})) for item in nodes])
                connection.executemany("INSERT INTO repository_code_graph_metadata_fts(graph_snapshot_id,node_id,path,name,qualified_name,signature,language,node_kind) VALUES(?,?,?,?,?,?,?,?)", [(graph_snapshot_id, item["node_id"], item.get("path") or "", item.get("name") or "", item.get("qualified_name") or "", item.get("signature") or "", item.get("language") or "", item.get("node_kind") or "") for item in nodes])
                connection.executemany("INSERT INTO repository_code_graph_edges(graph_snapshot_id,edge_id,stable_key,edge_kind,source_node_id,target_node_id,confidence,unresolved,extractor_version,evidence_json,attributes_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)", [(graph_snapshot_id, item["edge_id"], item["stable_key"], item["edge_kind"], item["source_node_id"], item["target_node_id"], item["confidence"], int(bool(item.get("unresolved"))), item.get("extractor_version") or CODE_GRAPH_EXTRACTOR_VERSION, _canonical_json(item.get("evidence") or {}), _canonical_json(item.get("attributes") or {})) for item in edges])
                connection.executemany("INSERT INTO repository_code_graph_parse_results(graph_snapshot_id,path,language,parser_id,parser_version,status,error_code,error_detail,content_sha256,node_count,edge_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)", [(graph_snapshot_id, item["path"], item["language"], item["parser_id"], item["parser_version"], item["status"], item.get("error_code") or "", item.get("error_detail") or "", item.get("content_sha256") or "", int(item.get("node_count") or 0), int(item.get("edge_count") or 0)) for item in parses])
                if fail_before_publish:
                    raise CodeGraphInjectedFailure("injected failure before current graph publication")
                connection.execute("UPDATE repository_code_graph_snapshots SET status='completed', completed_at=? WHERE graph_snapshot_id=?", (_utc_now(), graph_snapshot_id))
                connection.execute("INSERT OR REPLACE INTO repository_code_graph_current(project,root_id,graph_snapshot_id,updated_at) VALUES(?,?,?,?)", (project, root_id, graph_snapshot_id, _utc_now()))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.snapshot(graph_snapshot_id, include_material=False)

    def search_metadata(self, graph_snapshot_id: str, query: str, *, limit: int = 32, offset: int = 0) -> list[dict[str, Any]]:
        """Search durable graph metadata through SQLite FTS5, never source text."""

        value = str(query or "").strip()
        if not value or len(value) > 240 or "\x00" in value:
            raise CodeGraphError("metadata search query must be non-empty, bounded and NUL-free")
        tokens = re.findall(r"[A-Za-z0-9_./:-]{1,64}", value)
        if not tokens:
            raise CodeGraphError("metadata search query has no searchable tokens")
        match = " AND ".join(f'"{token.replace(chr(34), "")}"' for token in tokens[:16])
        bounded_limit = max(1, min(int(limit), 128))
        bounded_offset = max(0, min(int(offset), 10_000))
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT node_id, path, name, qualified_name, signature, language, node_kind,
                       bm25(repository_code_graph_metadata_fts) AS rank
                FROM repository_code_graph_metadata_fts
                WHERE graph_snapshot_id = ? AND repository_code_graph_metadata_fts MATCH ?
                ORDER BY rank, path, name, node_id
                LIMIT ? OFFSET ?
                """,
                (graph_snapshot_id, match, bounded_limit, bounded_offset),
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "node_id": row["node_id"],
                "path": row["path"],
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "signature": row["signature"],
                "language": row["language"],
                "node_kind": row["node_kind"],
                "score": round(-float(row["rank"] or 0.0), 8),
                "match_kind": "metadata",
            }
            for row in rows
        ]
