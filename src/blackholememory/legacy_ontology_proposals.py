"""Read-only schema proposals from explicit legacy semantic-graph relations."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .ontology_registry import OntologyEntityType
from .ontology_registry import OntologyRelationType
from .ontology_registry import OntologySchema
from .semantic_graph_migration_plan import load_semantic_graph_migration_inputs


SCHEMA_VERSION = "bhm.legacy-ontology-proposals.v1"
_MAX_EDGES = 10_000


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _id_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_legacy_ontology_proposals(
    edges: tuple[Mapping[str, str], ...],
    endpoints: Mapping[str, tuple[str, str]],
    active_schemas: Mapping[str, OntologySchema | None],
) -> dict[str, Any]:
    """Propose only exact ``DEPENDS_ON`` schemas; never activate or write one."""

    if len(edges) > _MAX_EDGES:
        raise ValueError("legacy semantic graph exceeds edge bound")
    reason_counts: Counter[str] = Counter()
    project_counts: dict[str, int] = defaultdict(int)
    active_counts: dict[str, int] = defaultdict(int)
    normalized: list[dict[str, str]] = []
    for raw in edges:
        source_id = str(raw.get("source_id") or "").strip()
        target_id = str(raw.get("target_id") or "").strip()
        relation = str(raw.get("legacy_relation") or "").strip().upper()
        if not source_id or not target_id or not relation:
            raise ValueError("legacy edge is incomplete")
        normalized.append({"source_id": source_id, "target_id": target_id, "legacy_relation": relation})
    for edge in sorted(normalized, key=lambda item: (item["source_id"], item["target_id"], item["legacy_relation"])):
        source = endpoints.get(edge["source_id"])
        target = endpoints.get(edge["target_id"])
        if source is None or target is None:
            reason_counts["endpoint_missing_from_sqlite"] += 1
        elif source[0] != target[0]:
            reason_counts["cross_project"] += 1
        elif source[1] != "active" or target[1] != "active":
            reason_counts["endpoint_not_active"] += 1
        elif edge["legacy_relation"] != "DEPENDS_ON":
            reason_counts["legacy_relation_requires_schema_decision"] += 1
        elif active_schemas.get(source[0]) is not None:
            active_counts[source[0]] += 1
            reason_counts["active_schema_already_present"] += 1
        else:
            project_counts[source[0]] += 1
            reason_counts["proposal_candidate"] += 1
    edge_digest = _digest(
        [
            {"source_id_digest": _id_digest(item["source_id"]), "target_id_digest": _id_digest(item["target_id"]), "legacy_relation": item["legacy_relation"]}
            for item in sorted(normalized, key=lambda item: (item["source_id"], item["target_id"], item["legacy_relation"]))
        ]
    )
    endpoint_digest = _digest(sorted((_id_digest(identifier), project, lifecycle) for identifier, (project, lifecycle) in endpoints.items()))
    proposals: list[dict[str, Any]] = []
    for project, count in sorted(project_counts.items()):
        schema = OntologySchema(
            project=project,
            revision=1,
            owner="operator-review-required",
            activation_status="proposal",
            entity_types=(OntologyEntityType(name="memory"),),
            relation_types=(OntologyRelationType(name="depends_on", source_types=("memory",), target_types=("memory",)),),
            provenance={"source": "legacy-semantic-graph-read-model", "legacy_graph_digest": edge_digest, "eligible_depends_on_count": str(count)},
        )
        proposals.append(
            {
                "project": project,
                "candidate_count": count,
                "schema": schema.canonical_payload(),
                "schema_digest": schema.digest(),
                "activation": "proposal_only",
                "persistence": "not_performed",
                "required_before_activation": ["operator_review", "explicit_persist", "explicit_activation", "relation_write_admission_smoke"],
            }
        )
    core = {
        "schema_version": SCHEMA_VERSION,
        "edge_count": len(normalized),
        "reason_counts": dict(sorted(reason_counts.items())),
        "active_schema_depends_on_counts": dict(sorted(active_counts.items())),
        "proposals": proposals,
        "bindings": {"legacy_graph_digest": edge_digest, "authority_snapshot_digest": endpoint_digest},
        "execution": {"read_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False, "schema_persisted": False, "schema_activated": False, "link_migration_apply": False, "raw_memory_ids_disclosed": False, "raw_content_disclosed": False},
    }
    return {**core, "proposal_digest": _digest(core)}


def build_live_legacy_ontology_proposals(
    database: Path | str,
    semantic_graph: Path | str,
) -> dict[str, Any]:
    """Read current local sources and return proposal-only ontology candidates."""

    edges, endpoints, active_schemas = load_semantic_graph_migration_inputs(database, semantic_graph)
    return build_legacy_ontology_proposals(edges, endpoints, active_schemas)


__all__ = ["SCHEMA_VERSION", "build_legacy_ontology_proposals", "build_live_legacy_ontology_proposals"]
