"""Deterministic repository conventions and architecture-memory cards.

WI-04 deliberately treats conventions as evidence-backed proposals.  The
module consumes the completed WI-02 SQLite code graph, stores only hashes,
stable keys and provenance, and requires an explicit reviewer before a card can
be accepted as an architecture decision.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .code_graph import CODE_GRAPH_EXTRACTOR_VERSION
from .code_graph import CODE_GRAPH_SCHEMA_VERSION
from .code_graph import SQLiteCodeGraphStore
from .repository_index import SQLiteRepositoryIndexStore


CONVENTION_SCHEMA_VERSION = "bhm.repository-conventions.v1"
CONVENTION_STORE_SCHEMA_VERSION = 1
CONVENTION_EXTRACTOR_VERSION = "bhm.repository-conventions.extractor.v1"
MAX_CONVENTION_CARDS = 64
MAX_CARD_EXAMPLES = 8
MAX_CARD_EVIDENCE = 64
CARD_STATUSES = frozenset({"proposal", "accepted", "rejected"})

_TABLES = {
    "repository_convention_meta",
    "repository_convention_snapshots",
    "repository_convention_cards",
    "repository_convention_examples",
    "repository_convention_current",
}


class ConventionMemoryError(ValueError):
    """Raised when a convention snapshot or review operation is invalid."""


class ConventionMemoryInjectedFailure(RuntimeError):
    """Test-only failure used to prove last-known-good publication."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clip(value: Any, limit: int = 1_000) -> str:
    return str(value or "").strip()[:limit]


def _stable_card_id(kind: str, statement: str) -> str:
    return f"card_{_sha256(f'{kind}:{statement}')[:24]}"


def _node_paths(nodes: list[Mapping[str, Any]]) -> set[str]:
    return {str(node.get("path") or "").replace("\\", "/") for node in nodes if str(node.get("path") or "").strip()}


def _is_architecture_reference_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    return normalized.startswith(("references/architecture/", "docs/adr/"))


def _source_refs(nodes: list[Mapping[str, Any]]) -> list[str]:
    refs = {
        str((node.get("provenance") or {}).get("source_ref") or "")
        for node in nodes
    }
    return sorted(ref for ref in refs if ref)[:MAX_CARD_EVIDENCE]


def _evidence(
    snapshot: Mapping[str, Any],
    nodes: list[Mapping[str, Any]],
    *,
    edges: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    paths = sorted(_node_paths(nodes))[:MAX_CARD_EVIDENCE]
    path_hashes = {
        str(node.get("path")): str(node.get("content_sha256") or "")
        for node in nodes
        if str(node.get("path") or "").strip() and str(node.get("content_sha256") or "").strip()
    }
    related_tests = sorted(path for path in paths if "/tests/" in f"/{path}" or Path(path).name.startswith("test_"))[:MAX_CARD_EVIDENCE]
    related_adrs = sorted(
        path
        for path in paths
        if _is_architecture_reference_path(path)
    )[:MAX_CARD_EVIDENCE]
    edge_keys = sorted(str(edge.get("stable_key") or "") for edge in (edges or []))[:MAX_CARD_EVIDENCE]
    edge_refs = {
        str(ref)
        for edge in (edges or [])
        for ref in (edge.get("evidence") or {}).get("source_refs", [])
        if str(ref).strip()
    }
    return {
        "authority": "sqlite-code-graph",
        "graph_schema_version": CODE_GRAPH_SCHEMA_VERSION,
        "graph_snapshot_id": str(snapshot.get("graph_snapshot_id") or ""),
        "graph_digest": str(snapshot.get("graph_digest") or ""),
        "extractor_version": CONVENTION_EXTRACTOR_VERSION,
        "parser_version": CODE_GRAPH_EXTRACTOR_VERSION,
        "node_keys": sorted(str(node.get("stable_key") or "") for node in nodes)[:MAX_CARD_EVIDENCE],
        "edge_keys": edge_keys,
        "source_refs": sorted(set(_source_refs(nodes)) | edge_refs)[:MAX_CARD_EVIDENCE],
        "path_hashes": dict(sorted(path_hashes.items())[:MAX_CARD_EVIDENCE]),
        "related_test_paths": related_tests,
        "related_adr_paths": related_adrs,
    }


def _rank_examples(nodes: list[Mapping[str, Any]], confidence: float) -> list[dict[str, Any]]:
    ranked = sorted(
        nodes,
        key=lambda node: (
            0 if node.get("node_kind") in {"function", "method", "class", "test", "route"} else 1,
            str(node.get("path") or ""),
            int(node.get("start_line") or 0),
            str(node.get("stable_key") or ""),
        ),
    )[:MAX_CARD_EXAMPLES]
    examples: list[dict[str, Any]] = []
    for index, node in enumerate(ranked, start=1):
        examples.append(
            {
                "node_key": str(node.get("stable_key") or ""),
                "path": str(node.get("path") or ""),
                "source_ref": str((node.get("provenance") or {}).get("source_ref") or ""),
                "role": str(node.get("node_kind") or "evidence"),
                "rank": index,
                "score": round(max(0.0, confidence - ((index - 1) * 0.03)), 4),
            }
        )
    return examples


def _card(
    snapshot: Mapping[str, Any],
    *,
    kind: str,
    title: str,
    statement: str,
    rationale: str,
    nodes: list[Mapping[str, Any]],
    edges: list[Mapping[str, Any]] | None = None,
    support: int | None = None,
    population: int | None = None,
) -> dict[str, Any]:
    support_count = int(support if support is not None else len(nodes))
    population_count = max(int(population if population is not None else len(nodes)), support_count, 1)
    confidence = round(min(0.99, max(0.0, support_count / population_count)), 4)
    evidence = _evidence(snapshot, nodes, edges=edges)
    card_id = _stable_card_id(kind, statement)
    return {
        "card_id": card_id,
        "card_kind": kind,
        "title": _clip(title, 240),
        "statement": _clip(statement, 1_500),
        "rationale": _clip(rationale, 1_500),
        "status": "proposal",
        "support_count": support_count,
        "population_count": population_count,
        "confidence": confidence,
        "freshness_score": 1.0,
        "stale": False,
        "evidence": evidence,
        "examples": _rank_examples(nodes, confidence),
        "review": {"reviewer": "", "decision": "proposal", "reason": "", "reviewed_at": None},
    }


def _symbol_name_style(name: str) -> str:
    value = str(name or "")
    if value.startswith("test_") or re.match(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$", value):
        return "snake_case"
    if re.match(r"^[A-Z][A-Za-z0-9]*$", value):
        return "PascalCase"
    if re.match(r"^[a-z][A-Za-z0-9]*$", value) and any(char.isupper() for char in value):
        return "camelCase"
    if re.match(r"^[A-Z][A-Z0-9_]+$", value):
        return "UPPER_CASE"
    return "other"


def _extract_cards(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = list(snapshot.get("nodes") or [])
    edges = list(snapshot.get("edges") or [])
    symbol_nodes = [
        node
        for node in nodes
        if node.get("node_kind") in {"class", "function", "method", "test"}
        and not bool((node.get("attributes") or {}).get("external"))
    ]
    cards: list[dict[str, Any]] = []

    styles = Counter(_symbol_name_style(str(node.get("name") or "")) for node in symbol_nodes)
    styles.pop("other", None)
    if styles:
        dominant, support_count = sorted(styles.items(), key=lambda item: (-item[1], item[0]))[0]
        examples = [node for node in symbol_nodes if _symbol_name_style(str(node.get("name") or "")) == dominant]
        cards.append(
            _card(
                snapshot,
                kind="naming",
                title="Dominant symbol naming style",
                statement=f"Repository symbols predominantly use {dominant}.",
                rationale=f"{support_count} of {sum(styles.values())} classified symbols match the dominant style; this remains a proposal until reviewed.",
                nodes=examples,
                support=support_count,
                population=sum(styles.values()),
            )
        )

    file_nodes = [node for node in nodes if node.get("node_kind") == "file"]
    test_files = [
        node
        for node in file_nodes
        if "/tests/" in f"/{str(node.get('path') or '').replace('\\', '/')}"
        or Path(str(node.get("path") or "")).name.startswith("test_")
    ]
    if test_files:
        cards.append(
            _card(
                snapshot,
                kind="test_layout",
                title="Test layout",
                statement="Tests are collected under a dedicated tests path or test_ files.",
                rationale="The graph contains explicit test files and tests edges; keep this as a reviewable convention rather than an inferred hard rule.",
                nodes=test_files,
                support=len(test_files),
                population=len(file_nodes),
            )
        )

    node_by_id = {str(node.get("node_id")): node for node in nodes}
    import_edges = [edge for edge in edges if edge.get("edge_kind") == "imports"]
    internal_imports = [
        edge
        for edge in import_edges
        if str((node_by_id.get(str(edge.get("source_node_id"))) or {}).get("path") or "")
        and str((node_by_id.get(str(edge.get("target_node_id"))) or {}).get("path") or "")
    ]
    if internal_imports:
        import_nodes = [node_by_id[node_id] for edge in internal_imports for node_id in (str(edge.get("source_node_id")), str(edge.get("target_node_id"))) if node_id in node_by_id]
        cards.append(
            _card(
                snapshot,
                kind="module_boundary",
                title="Internal module boundaries are graph-visible",
                statement="Internal imports are represented as typed, provenance-carrying graph edges.",
                rationale=f"{len(internal_imports)} internal import edges connect {len({str(node.get('path') or '') for node in import_nodes})} repository files.",
                nodes=import_nodes,
                edges=internal_imports,
                support=len(internal_imports),
                population=max(len(import_edges), 1),
            )
        )

    route_nodes = [node for node in nodes if node.get("node_kind") == "route"]
    route_edges = [edge for edge in edges if edge.get("edge_kind") == "route_handles"]
    if route_nodes:
        cards.append(
            _card(
                snapshot,
                kind="api_routes",
                title="API routes use explicit method/path graph nodes",
                statement="HTTP routes are represented as method/path nodes linked to handlers by route_handles edges.",
                rationale="Route evidence is structural and reviewable; it is not promoted to an auth or API policy automatically.",
                nodes=route_nodes,
                edges=route_edges,
                support=len(route_nodes),
                population=max(len(route_nodes), 1),
            )
        )

    adr_nodes = [
        node
        for node in nodes
        if _is_architecture_reference_path(str(node.get("path") or ""))
    ]
    if adr_nodes:
        cards.append(
            _card(
                snapshot,
                kind="architecture_authority",
                title="Architecture references are evidence",
                statement="Architecture decisions are anchored in reviewed reference evidence before becoming authority.",
                rationale=f"{len(adr_nodes)} graph nodes point into the repository architecture reference path.",
                nodes=adr_nodes,
                support=len(adr_nodes),
                population=max(len(file_nodes), 1),
            )
        )

    config_nodes = [
        node
        for node in file_nodes
        if any(token in str(node.get("path") or "").replace("\\", "/").casefold() for token in ("config/", "runtime/", "scripts/", "dockerfile", "compose"))
    ]
    if config_nodes:
        cards.append(
            _card(
                snapshot,
                kind="operations",
                title="Operational configuration is path-scoped",
                statement="Runtime, configuration, scripts and deployment evidence are kept in explicit operational paths.",
                rationale="The card records path evidence only; it does not execute or infer deployment authority.",
                nodes=config_nodes,
                support=len(config_nodes),
                population=max(len(file_nodes), 1),
            )
        )

    error_nodes = [
        node
        for node in symbol_nodes
        if re.search(r"(error|exception|validate|redact|guard|retry|rollback)", str(node.get("name") or ""), re.IGNORECASE)
    ]
    if error_nodes:
        cards.append(
            _card(
                snapshot,
                kind="safety_patterns",
                title="Safety and validation symbols are explicit",
                statement="Validation, guard, retry and rollback symbols are named and graph-visible.",
                rationale="Frequency is evidence for review, not permission to change security or approval policy.",
                nodes=error_nodes,
                support=len(error_nodes),
                population=max(len(symbol_nodes), 1),
            )
        )
    return cards[:MAX_CONVENTION_CARDS]


def extract_convention_memory(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Extract deterministic proposal cards from one materialized graph snapshot."""

    graph_snapshot_id = str(snapshot.get("graph_snapshot_id") or "")
    graph_digest = str(snapshot.get("graph_digest") or "")
    if not graph_snapshot_id or not graph_digest:
        raise ConventionMemoryError("completed graph snapshot with digest is required")
    cards = _extract_cards(snapshot)
    core = {
        "schema_version": CONVENTION_SCHEMA_VERSION,
        "extractor_version": CONVENTION_EXTRACTOR_VERSION,
        "graph_snapshot_id": graph_snapshot_id,
        "graph_digest": graph_digest,
        "card_ids": [str(card["card_id"]) for card in cards],
        "cards_digest": _sha256(_canonical_json(cards)),
    }
    convention_digest = _sha256(_canonical_json(core))
    return {
        **core,
        "convention_snapshot_id": f"conventions_{convention_digest[:24]}",
        "convention_digest": convention_digest,
        "project": str(snapshot.get("project") or ""),
        "root_id": str(snapshot.get("root_id") or ""),
        "repository_snapshot_id": str(snapshot.get("repository_snapshot_id") or ""),
        "cards": cards,
        "summary": {
            "card_count": len(cards),
            "proposal_count": sum(card.get("status") == "proposal" for card in cards),
            "accepted_count": sum(card.get("status") == "accepted" for card in cards),
            "rejected_count": sum(card.get("status") == "rejected" for card in cards),
            "kinds": dict(sorted(Counter(str(card.get("card_kind") or "") for card in cards).items())),
        },
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_mem0": False,
            "model_started": False,
            "raw_source_returned": False,
            "llm_authority": "proposal-only",
        },
    }


class SQLiteConventionMemoryStore:
    """Durable WI-04 cards in the canonical SQLite database."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path).expanduser().resolve()
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5.0)
        else:
            connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def inspect_schema(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"database_exists": False, "schema_version": None, "ready": False, "tables": [], "missing_tables": sorted(_TABLES), "quick_check": None, "row_counts": {}}
        connection = self._connect(read_only=True)
        try:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            present = sorted(table for table in tables if table.startswith("repository_convention_"))
            version = None
            if "repository_convention_meta" in tables:
                row = connection.execute("SELECT value FROM repository_convention_meta WHERE key='schema_version'").fetchone()
                version = int(row["value"]) if row else None
            counts = {table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in present}
            quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            missing = sorted(_TABLES - tables)
            return {"database_exists": True, "schema_version": version, "ready": version == CONVENTION_STORE_SCHEMA_VERSION and not missing and quick == "ok", "tables": present, "missing_tables": missing, "quick_check": quick, "row_counts": counts}
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            if not SQLiteRepositoryIndexStore(self.path).inspect_schema().get("ready"):
                raise ConventionMemoryError("WI-01 repository-index schema must be ready")
            if not SQLiteCodeGraphStore(self.path).inspect_schema().get("ready"):
                raise ConventionMemoryError("WI-02 code-graph schema must be ready")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS repository_convention_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS repository_convention_snapshots (
                        convention_snapshot_id TEXT PRIMARY KEY,
                        graph_snapshot_id TEXT NOT NULL,
                        repository_snapshot_id TEXT NOT NULL,
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        graph_digest TEXT NOT NULL,
                        convention_digest TEXT NOT NULL UNIQUE,
                        extractor_version TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('building','completed','failed')),
                        summary_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_convention_graph_snapshot
                        ON repository_convention_snapshots(graph_snapshot_id, extractor_version);
                    CREATE TABLE IF NOT EXISTS repository_convention_cards (
                        convention_snapshot_id TEXT NOT NULL,
                        card_id TEXT NOT NULL,
                        card_kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        statement TEXT NOT NULL,
                        rationale TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('proposal','accepted','rejected')),
                        support_count INTEGER NOT NULL,
                        population_count INTEGER NOT NULL,
                        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                        freshness_score REAL NOT NULL CHECK(freshness_score >= 0 AND freshness_score <= 1),
                        stale INTEGER NOT NULL CHECK(stale IN (0,1)),
                        evidence_json TEXT NOT NULL,
                        review_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        reviewed_at TEXT,
                        PRIMARY KEY(convention_snapshot_id, card_id),
                        FOREIGN KEY(convention_snapshot_id) REFERENCES repository_convention_snapshots(convention_snapshot_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS repository_convention_examples (
                        convention_snapshot_id TEXT NOT NULL,
                        card_id TEXT NOT NULL,
                        rank INTEGER NOT NULL,
                        node_key TEXT NOT NULL,
                        path TEXT NOT NULL,
                        source_ref TEXT NOT NULL,
                        role TEXT NOT NULL,
                        score REAL NOT NULL,
                        PRIMARY KEY(convention_snapshot_id, card_id, rank),
                        FOREIGN KEY(convention_snapshot_id, card_id) REFERENCES repository_convention_cards(convention_snapshot_id, card_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS repository_convention_current (
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        convention_snapshot_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(project, root_id),
                        FOREIGN KEY(convention_snapshot_id) REFERENCES repository_convention_snapshots(convention_snapshot_id)
                    );
                    """
                )
                stored = connection.execute("SELECT value FROM repository_convention_meta WHERE key='schema_version'").fetchone()
                if stored is not None and int(stored["value"]) != CONVENTION_STORE_SCHEMA_VERSION:
                    raise ConventionMemoryError(f"unsupported convention schema {stored['value']}")
                connection.execute("INSERT OR REPLACE INTO repository_convention_meta(key,value) VALUES('schema_version',?)", (str(CONVENTION_STORE_SCHEMA_VERSION),))
                connection.execute("INSERT OR IGNORE INTO repository_convention_meta(key,value) VALUES('extractor_version',?)", (CONVENTION_EXTRACTOR_VERSION,))
                connection.commit()
                self._initialized = True
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def _payload(connection: sqlite3.Connection, row: sqlite3.Row, *, include_cards: bool) -> dict[str, Any]:
        payload = dict(row)
        payload["summary"] = json.loads(str(payload.pop("summary_json")))
        if include_cards:
            cards: list[dict[str, Any]] = []
            rows = connection.execute("SELECT * FROM repository_convention_cards WHERE convention_snapshot_id=? ORDER BY card_kind, card_id", (row["convention_snapshot_id"],)).fetchall()
            for item in rows:
                card = dict(item)
                card["stale"] = bool(card["stale"])
                card["evidence"] = json.loads(str(card.pop("evidence_json")))
                card["review"] = json.loads(str(card.pop("review_json")))
                card["examples"] = [dict(example) for example in connection.execute("SELECT rank,node_key,path,source_ref,role,score FROM repository_convention_examples WHERE convention_snapshot_id=? AND card_id=? ORDER BY rank", (row["convention_snapshot_id"], card["card_id"])).fetchall()]
                cards.append(card)
            payload["cards"] = cards
        return payload

    def current_snapshot(self, project: str, root_id: str, *, include_cards: bool = False) -> dict[str, Any] | None:
        schema = self.inspect_schema()
        if not schema.get("ready"):
            return None
        connection = self._connect(read_only=True)
        try:
            row = connection.execute("SELECT snapshot.* FROM repository_convention_current AS current JOIN repository_convention_snapshots AS snapshot ON snapshot.convention_snapshot_id=current.convention_snapshot_id WHERE current.project=? AND current.root_id=?", (project, root_id)).fetchone()
            return self._payload(connection, row, include_cards=include_cards) if row else None
        finally:
            connection.close()

    def snapshot(self, convention_snapshot_id: str, *, include_cards: bool = False, read_only: bool = False) -> dict[str, Any]:
        if read_only:
            if not self.inspect_schema().get("ready"):
                raise ConventionMemoryError("convention schema is not ready")
        else:
            self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute("SELECT * FROM repository_convention_snapshots WHERE convention_snapshot_id=?", (convention_snapshot_id,)).fetchone()
            if row is None:
                raise ConventionMemoryError(f"convention snapshot not found: {convention_snapshot_id}")
            return self._payload(connection, row, include_cards=include_cards)
        finally:
            connection.close()

    def publish(self, material: Mapping[str, Any], *, fail_before_publish: bool = False) -> dict[str, Any]:
        self.initialize()
        snapshot_id = str(material.get("convention_snapshot_id") or "")
        if not snapshot_id:
            raise ConventionMemoryError("convention snapshot id is required")
        with self._lock:
            connection = self._connect()
            try:
                existing = connection.execute("SELECT * FROM repository_convention_snapshots WHERE convention_digest=?", (material.get("convention_digest"),)).fetchone()
                if existing is not None:
                    connection.close()
                    return self.snapshot(str(existing["convention_snapshot_id"]), include_cards=True)
                connection.execute("BEGIN IMMEDIATE")
                created = _utc_now()
                connection.execute("INSERT INTO repository_convention_snapshots(convention_snapshot_id,graph_snapshot_id,repository_snapshot_id,project,root_id,graph_digest,convention_digest,extractor_version,status,summary_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (snapshot_id, material.get("graph_snapshot_id"), material.get("repository_snapshot_id"), material.get("project"), material.get("root_id"), material.get("graph_digest"), material.get("convention_digest"), material.get("extractor_version"), "building", _canonical_json(material.get("summary") or {}), created))
                for card in list(material.get("cards") or []):
                    connection.execute("INSERT INTO repository_convention_cards(convention_snapshot_id,card_id,card_kind,title,statement,rationale,status,support_count,population_count,confidence,freshness_score,stale,evidence_json,review_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (snapshot_id, card.get("card_id"), card.get("card_kind"), card.get("title"), card.get("statement"), card.get("rationale"), card.get("status"), int(card.get("support_count") or 0), int(card.get("population_count") or 0), float(card.get("confidence") or 0.0), float(card.get("freshness_score") or 0.0), int(bool(card.get("stale"))), _canonical_json(card.get("evidence") or {}), _canonical_json(card.get("review") or {}), created))
                    for example in list(card.get("examples") or [])[:MAX_CARD_EXAMPLES]:
                        connection.execute("INSERT INTO repository_convention_examples(convention_snapshot_id,card_id,rank,node_key,path,source_ref,role,score) VALUES(?,?,?,?,?,?,?,?)", (snapshot_id, card.get("card_id"), int(example.get("rank") or 0), example.get("node_key"), example.get("path") or "", example.get("source_ref") or "", example.get("role") or "evidence", float(example.get("score") or 0.0)))
                if fail_before_publish:
                    raise ConventionMemoryInjectedFailure("injected failure before convention current publish")
                connection.execute("UPDATE repository_convention_snapshots SET status='completed', completed_at=? WHERE convention_snapshot_id=?", (_utc_now(), snapshot_id))
                connection.execute("INSERT OR REPLACE INTO repository_convention_current(project,root_id,convention_snapshot_id,updated_at) VALUES(?,?,?,?)", (material.get("project"), material.get("root_id"), snapshot_id, _utc_now()))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.snapshot(snapshot_id, include_cards=True)

    def review_card(self, *, project: str, root_id: str, card_id: str, decision: str, reviewer: str, reason: str) -> dict[str, Any]:
        decision = str(decision or "").strip().casefold()
        reviewer = _clip(reviewer, 240)
        reason = _clip(reason, 1_000)
        if decision not in CARD_STATUSES:
            raise ConventionMemoryError(f"unsupported card decision: {decision}")
        if decision != "proposal" and (not reviewer or not reason):
            raise ConventionMemoryError("reviewer and reason are required for accept/reject")
        current = self.current_snapshot(project, root_id, include_cards=True)
        if current is None:
            raise ConventionMemoryError("current convention snapshot unavailable")
        cards = {str(card.get("card_id")): card for card in current.get("cards") or []}
        if card_id not in cards:
            raise ConventionMemoryError(f"convention card not found: {card_id}")
        review = {"reviewer": reviewer, "decision": decision, "reason": reason, "reviewed_at": _utc_now() if decision != "proposal" else None}
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE repository_convention_cards SET status=?, review_json=?, reviewed_at=? WHERE convention_snapshot_id=? AND card_id=?", (decision, _canonical_json(review), review["reviewed_at"], current["convention_snapshot_id"], card_id))
            status_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM repository_convention_cards WHERE convention_snapshot_id=? GROUP BY status",
                (current["convention_snapshot_id"],),
            ).fetchall()
            summary = dict(current.get("summary") or {})
            status_counts = {str(row["status"]): int(row["count"]) for row in status_rows}
            summary["card_count"] = sum(status_counts.values())
            summary["proposal_count"] = status_counts.get("proposal", 0)
            summary["accepted_count"] = status_counts.get("accepted", 0)
            summary["rejected_count"] = status_counts.get("rejected", 0)
            connection.execute(
                "UPDATE repository_convention_snapshots SET summary_json=? WHERE convention_snapshot_id=?",
                (_canonical_json(summary), current["convention_snapshot_id"]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.current_snapshot(project, root_id, include_cards=True) or current


def preview_convention_memory(database_path: str | Path, *, project: str, root_id: str, graph_snapshot_id: str | None = None) -> dict[str, Any]:
    """Read-only preview over a graph snapshot; never initializes convention tables."""

    graph_store = SQLiteCodeGraphStore(database_path)
    current = graph_store.current_snapshot(project, root_id, include_material=False)
    selected = graph_snapshot_id or (str(current.get("graph_snapshot_id")) if current else "")
    if not selected:
        raise ConventionMemoryError("graph snapshot unavailable; build WI-02 graph first")
    snapshot = graph_store.snapshot(selected, include_material=True, read_only=True)
    result = extract_convention_memory(snapshot)
    result["stale"] = bool(current and current.get("graph_snapshot_id") != snapshot.get("graph_snapshot_id"))
    result["execution"] = {**result["execution"], "writes_sqlite_state": False}
    return result


def build_convention_memory(database_path: str | Path, *, project: str, root_id: str, graph_snapshot_id: str | None = None, fail_before_publish: bool = False) -> dict[str, Any]:
    """Extract and transactionally publish convention cards."""

    graph_store = SQLiteCodeGraphStore(database_path)
    current = graph_store.current_snapshot(project, root_id, include_material=False)
    selected = graph_snapshot_id or (str(current.get("graph_snapshot_id")) if current else "")
    if not selected:
        raise ConventionMemoryError("graph snapshot unavailable; build WI-02 graph first")
    snapshot = graph_store.snapshot(selected, include_material=True)
    material = extract_convention_memory(snapshot)
    published = SQLiteConventionMemoryStore(database_path).publish(material, fail_before_publish=fail_before_publish)
    return {"schema_version": CONVENTION_SCHEMA_VERSION, "ok": True, "action": "build", "project": project, "root_id": root_id, "graph_snapshot_id": selected, "convention_snapshot_id": published.get("convention_snapshot_id"), "convention_digest": published.get("convention_digest"), "summary": published.get("summary") or material.get("summary"), "cards": published.get("cards") or [], "stale": bool(current and current.get("graph_snapshot_id") != selected), "execution": {"writes_sqlite_state": True, "writes_qdrant": False, "writes_mem0": False, "model_started": False, "raw_source_returned": False, "llm_authority": "proposal-only"}}


def explain_convention_card(database_path: str | Path, *, project: str, root_id: str, card_id: str) -> dict[str, Any]:
    store = SQLiteConventionMemoryStore(database_path)
    current = store.current_snapshot(project, root_id, include_cards=True)
    if current is None:
        raise ConventionMemoryError("current convention snapshot unavailable")
    card = next((item for item in current.get("cards") or [] if str(item.get("card_id")) == card_id), None)
    if card is None:
        raise ConventionMemoryError(f"convention card not found: {card_id}")
    graph_current = SQLiteCodeGraphStore(database_path).current_snapshot(project, root_id, include_material=False)
    stale = bool(graph_current and graph_current.get("graph_snapshot_id") != current.get("graph_snapshot_id"))
    card["freshness"] = {"state": "stale" if stale else "fresh", "score": 0.0 if stale else 1.0, "graph_snapshot_id": current.get("graph_snapshot_id"), "current_graph_snapshot_id": graph_current.get("graph_snapshot_id") if graph_current else None}
    return {"schema_version": CONVENTION_SCHEMA_VERSION, "card": card, "convention_snapshot_id": current.get("convention_snapshot_id"), "graph_snapshot_id": current.get("graph_snapshot_id"), "graph_digest": current.get("graph_digest"), "stale": stale, "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_mem0": False, "model_started": False, "raw_source_returned": False, "llm_authority": "proposal-only"}}


__all__ = [
    "CARD_STATUSES",
    "CONVENTION_EXTRACTOR_VERSION",
    "CONVENTION_SCHEMA_VERSION",
    "CONVENTION_STORE_SCHEMA_VERSION",
    "ConventionMemoryError",
    "ConventionMemoryInjectedFailure",
    "SQLiteConventionMemoryStore",
    "build_convention_memory",
    "explain_convention_card",
    "extract_convention_memory",
    "preview_convention_memory",
]
