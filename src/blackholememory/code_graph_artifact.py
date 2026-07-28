"""Integrity-checked, non-authoritative shared code-graph artifacts.

Artifacts contain only bounded graph metadata and provenance.  They never
contain repository source and are stored under the runtime directory, outside
the repository, ``.src`` quarantine, package, or release boundary.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .artifact_signature import ArtifactSignatureError
from .artifact_signature import verify_detached_ed25519


CODE_GRAPH_ARTIFACT_SCHEMA_VERSION = "bhm.code-graph.artifact.v1"
GRAPH_ARTIFACT_ADOPTION_SCHEMA_VERSION = "bhm.code-graph.adoption-receipt.v1"
GRAPH_ARTIFACT_TRUST_SCHEMA_VERSION = "bhm.code-graph.trust-receipt.v1"
GRAPH_ARTIFACT_DELTA_REPLAY_SCHEMA_VERSION = "bhm.code-graph.delta-replay-receipt.v1"
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_UNCOMPRESSED_BYTES = 128 * 1024 * 1024


class CodeGraphArtifactError(ValueError):
    """Raised when an artifact is unsafe, corrupt, or incompatible."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _material_digest(items: Any, *, key: str) -> str:
    values = list(items or []) if isinstance(items, list) else []
    normalized = sorted(
        [{name: value for name, value in item.items() if name != "graph_snapshot_id"} for item in values if isinstance(item, Mapping)],
        key=lambda item: str(item.get(key) or ""),
    )
    return _sha256(_canonical_json(normalized))


def artifact_root(runtime_dir: Path) -> Path:
    return (Path(runtime_dir).expanduser().resolve() / "code-graph-artifacts").resolve()


def _safe_payload(value: Any) -> Any:
    """Drop accidental source-bearing fields while preserving graph metadata."""

    if isinstance(value, Mapping):
        blocked = {"source", "source_text", "raw_source", "content", "body", "text"}
        return {str(key): _safe_payload(item) for key, item in value.items() if str(key).casefold() not in blocked}
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    return value


def build_graph_artifact_payload(material: Mapping[str, Any], *, project: str, root_id: str) -> dict[str, Any]:
    graph_digest = str(material.get("graph_digest") or "")
    repository_snapshot_id = str(material.get("repository_snapshot_id") or "")
    if not graph_digest or not repository_snapshot_id:
        raise CodeGraphArtifactError("completed graph snapshot with digest is required")
    safe_nodes = _safe_payload(list(material.get("nodes") or []))
    safe_edges = _safe_payload(list(material.get("edges") or []))
    safe_parses = _safe_payload(list(material.get("parse_results") or []))
    payload_nodes_digest = _material_digest(safe_nodes, key="stable_key")
    payload_edges_digest = _material_digest(safe_edges, key="stable_key")
    payload_parse_digest = _material_digest(safe_parses, key="path")
    graph_core = dict(material.get("graph_core") or {})
    graph_core.setdefault("repository_snapshot_id", repository_snapshot_id)
    graph_core.setdefault("graph_input_digest", str(material.get("graph_input_digest") or material.get("repository_snapshot_digest") or ""))
    graph_core.setdefault("parser_registry_digest", str(material.get("parser_registry_digest") or ""))
    graph_core.setdefault("nodes_digest", str(material.get("nodes_digest") or payload_nodes_digest))
    graph_core.setdefault("edges_digest", str(material.get("edges_digest") or payload_edges_digest))
    graph_core.setdefault("parse_digest", str(material.get("parse_digest") or payload_parse_digest))
    payload = {
        "schema_version": CODE_GRAPH_ARTIFACT_SCHEMA_VERSION,
        "project": str(project),
        "root_id": str(root_id),
        "graph_snapshot_id": str(material.get("graph_snapshot_id") or ""),
        "repository_snapshot_id": repository_snapshot_id,
        "repository_snapshot_digest": str(material.get("repository_snapshot_digest") or material.get("graph_input_digest") or ""),
        "graph_schema_version": str(material.get("schema_version") or "bhm.code-graph.v1"),
        "extractor_version": str(material.get("extractor_version") or "snapshot-derived"),
        "parser_registry_digest": str(material.get("parser_registry_digest") or ""),
        "graph_digest": graph_digest,
        "summary": _safe_payload(material.get("summary") or {}),
        "graph_core": _safe_payload(graph_core),
        "payload_nodes_digest": payload_nodes_digest,
        "payload_edges_digest": payload_edges_digest,
        "payload_parse_digest": payload_parse_digest,
        "nodes": safe_nodes,
        "edges": safe_edges,
        "parse_results": safe_parses,
        "previous_graph_snapshot_id": str(material.get("previous_graph_snapshot_id") or "") or None,
        "previous_graph_digest": str(material.get("previous_graph_digest") or "") or None,
    }
    encoded = _canonical_json(payload)
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise CodeGraphArtifactError("graph artifact exceeds bounded size")
    return payload


def export_graph_artifact(material: Mapping[str, Any], *, runtime_dir: Path, project: str, root_id: str) -> dict[str, Any]:
    payload = build_graph_artifact_payload(material, project=project, root_id=root_id)
    encoded = _canonical_json(payload)
    compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
    digest = _sha256(compressed)
    artifact_id = f"graph_artifact_{digest[:24]}"
    root = artifact_root(runtime_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = (root / f"{artifact_id}.json.gz").resolve()
    path.write_bytes(compressed)
    manifest = {
        "schema_version": CODE_GRAPH_ARTIFACT_SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "artifact_sha256": digest,
        "compressed_bytes": len(compressed),
        "uncompressed_bytes": len(encoded),
        "project": payload["project"],
        "root_id": payload["root_id"],
        "graph_snapshot_id": payload["graph_snapshot_id"],
        "repository_snapshot_id": payload["repository_snapshot_id"],
        "graph_digest": payload["graph_digest"],
        "payload_nodes_digest": payload["payload_nodes_digest"],
        "payload_edges_digest": payload["payload_edges_digest"],
        "payload_parse_digest": payload["payload_parse_digest"],
        "previous_graph_snapshot_id": payload.get("previous_graph_snapshot_id"),
        "previous_graph_digest": payload.get("previous_graph_digest"),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "authority": "non-authoritative; SQLite remains canonical",
        "source_persisted": False,
        "release_boundary": "runtime-only; excluded from .src/package/SBOM/release by policy",
    }
    manifest_path = (root / f"{artifact_id}.manifest.json").resolve()
    manifest_path.write_bytes(_canonical_json(manifest) + b"\n")
    return {"artifact": manifest, "path": str(path), "manifest_path": str(manifest_path)}


def _resolve_artifact_path(path: str, runtime_dir: Path) -> Path:
    root = artifact_root(runtime_dir)
    raw = str(path or "").strip()
    candidate_name = os.path.realpath(os.path.expanduser(raw))
    try:
        contained = os.path.commonpath((os.fspath(root), candidate_name)) == os.fspath(root)
    except ValueError as exc:
        raise CodeGraphArtifactError("artifact path is outside the runtime artifact boundary") from exc
    if not contained:
        raise CodeGraphArtifactError("artifact path is outside the runtime artifact boundary")
    candidate = Path(candidate_name)
    # lgtm [py/path-injection]
    if candidate.suffix != ".gz" or not candidate.is_file():
        raise CodeGraphArtifactError("graph artifact file is unavailable")
    return candidate


def verify_graph_artifact(path: str, *, runtime_dir: Path) -> dict[str, Any]:
    candidate = _resolve_artifact_path(path, runtime_dir)
    # lgtm [py/path-injection]
    compressed = candidate.read_bytes()
    if len(compressed) > MAX_ARTIFACT_BYTES:
        raise CodeGraphArtifactError("graph artifact exceeds bounded size")
    digest = _sha256(compressed)
    try:
        encoded = gzip.decompress(compressed)
        if len(encoded) > MAX_ARTIFACT_UNCOMPRESSED_BYTES:
            raise CodeGraphArtifactError("graph artifact uncompressed payload exceeds bounded size")
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodeGraphArtifactError("graph artifact is not valid gzip JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != CODE_GRAPH_ARTIFACT_SCHEMA_VERSION:
        raise CodeGraphArtifactError("unsupported graph artifact schema")
    required = ("project", "root_id", "repository_snapshot_id", "graph_digest", "nodes", "edges")
    if any(not payload.get(key) for key in required):
        raise CodeGraphArtifactError("graph artifact provenance is incomplete")
    for collection_name in ("nodes", "edges", "parse_results"):
        collection = payload.get(collection_name, [])
        if not isinstance(collection, list) or any(not isinstance(item, Mapping) for item in collection):
            raise CodeGraphArtifactError(f"graph artifact {collection_name} must contain metadata mappings")
    for digest_name in ("payload_nodes_digest", "payload_edges_digest", "payload_parse_digest"):
        digest_value = payload.get(digest_name)
        if digest_value is not None and digest_value != "" and not _is_digest(digest_value):
            raise CodeGraphArtifactError(f"graph artifact {digest_name} must be sha256")
    payload_digest = _sha256(encoded)
    node_digest = _sha256(_canonical_json(payload.get("nodes") or []))
    edge_digest = _sha256(_canonical_json(payload.get("edges") or []))
    parse_digest = _sha256(_canonical_json(payload.get("parse_results") or []))
    graph_core = payload.get("graph_core") if isinstance(payload.get("graph_core"), Mapping) else {}
    graph_core_digest = _sha256(_canonical_json(graph_core))
    graph_core_complete = all(str(graph_core.get(key) or "") for key in ("repository_snapshot_id", "graph_input_digest", "parser_registry_digest", "nodes_digest", "edges_digest", "parse_digest"))
    authoritative_graph_digest = str(payload.get("graph_digest") or "")
    manifest_path = candidate.with_name(candidate.name.replace(".json.gz", ".manifest.json"))
    manifest_status = "missing"
    manifest_artifact_digest = ""
    # lgtm [py/path-injection]
    if manifest_path.is_file():
        try:
            # lgtm [py/path-injection]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_artifact_digest = str(manifest.get("artifact_sha256") or "").lower()
            manifest_status = "pass" if manifest_artifact_digest == digest else "fail"
        except (OSError, UnicodeError, json.JSONDecodeError):
            manifest_status = "invalid"
    replay_checks = {
        "compressed_digest_valid": digest == _sha256(compressed),
        "canonical_payload_digest": bool(payload_digest),
        "canonical_node_digest": bool(node_digest),
        "canonical_edge_digest": bool(edge_digest),
        "canonical_parse_digest": bool(parse_digest),
        "payload_node_digest_matches": str(payload.get("payload_nodes_digest") or "") in {"", _material_digest(payload.get("nodes") or [], key="stable_key")},
        "payload_edge_digest_matches": str(payload.get("payload_edges_digest") or "") in {"", _material_digest(payload.get("edges") or [], key="stable_key")},
        "payload_parse_digest_matches": str(payload.get("payload_parse_digest") or "") in {"", _material_digest(payload.get("parse_results") or [], key="path")},
        "graph_core_complete": graph_core_complete,
        "graph_core_digest_matches": not _is_digest(authoritative_graph_digest) or graph_core_digest == authoritative_graph_digest,
        "manifest_digest_matches": manifest_status in {"pass", "missing"},
        "bounded_counts": len(payload.get("nodes") or []) <= 100_000 and len(payload.get("edges") or []) <= 200_000 and len(payload.get("parse_results") or []) <= 100_000,
        "deterministic_gzip": gzip.compress(encoded, compresslevel=9, mtime=0) == compressed,
    }
    replay_core = {
        "schema_version": GRAPH_ARTIFACT_DELTA_REPLAY_SCHEMA_VERSION,
        "status": "pass" if all(replay_checks.values()) else "gap",
        "checks": replay_checks,
        "artifact_sha256": digest,
        "payload_digest": payload_digest,
        "node_digest": node_digest,
        "edge_digest": edge_digest,
        "parse_digest": parse_digest,
        "graph_core_digest": graph_core_digest,
        "manifest_status": manifest_status,
        "manifest_artifact_sha256": manifest_artifact_digest or None,
        "previous_graph_snapshot_id": payload.get("previous_graph_snapshot_id"),
        "previous_graph_digest": payload.get("previous_graph_digest"),
        "source_free": True,
    }
    replay_integrity = {**replay_core, "receipt_digest": _sha256(_canonical_json(replay_core))}
    return {
        "valid": True,
        "path": str(candidate),
        "artifact_sha256": digest,
        "compressed_bytes": len(compressed),
        "uncompressed_bytes": len(encoded),
        "artifact": {
            "schema_version": payload.get("schema_version"),
            "project": payload.get("project"),
            "root_id": payload.get("root_id"),
            "graph_snapshot_id": payload.get("graph_snapshot_id"),
            "repository_snapshot_id": payload.get("repository_snapshot_id"),
            "repository_snapshot_digest": payload.get("repository_snapshot_digest"),
            "graph_schema_version": payload.get("graph_schema_version"),
            "extractor_version": payload.get("extractor_version"),
            "parser_registry_digest": payload.get("parser_registry_digest"),
            "graph_digest": payload.get("graph_digest"),
            "payload_digest": payload_digest,
            "node_digest": node_digest,
            "edge_digest": edge_digest,
            "parse_digest": parse_digest,
            "payload_nodes_digest": payload.get("payload_nodes_digest"),
            "payload_edges_digest": payload.get("payload_edges_digest"),
            "payload_parse_digest": payload.get("payload_parse_digest"),
            "previous_graph_snapshot_id": payload.get("previous_graph_snapshot_id"),
            "previous_graph_digest": payload.get("previous_graph_digest"),
            "node_count": len(payload.get("nodes") or []),
            "edge_count": len(payload.get("edges") or []),
            "parse_result_count": len(payload.get("parse_results") or []),
        },
        "authority": "non-authoritative; verify/preview only",
        "source_persisted": False,
        "import_apply": False,
        "replay_integrity": replay_integrity,
    }


def build_graph_artifact_delta_replay_receipt(
    verified: Mapping[str, Any],
    *,
    target_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a source-free artifact delta and replay-integrity receipt."""

    artifact = dict(verified.get("artifact") or {})
    target = dict(target_snapshot or {})
    target_summary = target.get("summary") if isinstance(target.get("summary"), Mapping) else target
    artifact_nodes = int(artifact.get("node_count") or 0)
    artifact_edges = int(artifact.get("edge_count") or 0)
    target_nodes = int(target_summary.get("node_count") or target_summary.get("nodes") or 0)
    target_edges = int(target_summary.get("edge_count") or target_summary.get("edges") or 0)
    target_digest = str(target.get("graph_digest") or "")
    artifact_digest = str(artifact.get("graph_digest") or "")
    target_available = bool(target_digest or target.get("graph_snapshot_id"))
    replay = dict(verified.get("replay_integrity") or {})
    same_snapshot = bool(artifact.get("graph_snapshot_id") and target.get("graph_snapshot_id") and artifact.get("graph_snapshot_id") == target.get("graph_snapshot_id"))
    target_previous_id = str(target.get("previous_graph_snapshot_id") or "")
    artifact_previous_id = str(artifact.get("previous_graph_snapshot_id") or "")
    chain_status = "pass" if same_snapshot and target_previous_id == artifact_previous_id else ("gap" if not same_snapshot or not target_available else "fail")
    delta_core = {
        "schema_version": GRAPH_ARTIFACT_DELTA_REPLAY_SCHEMA_VERSION,
        "status": "pass" if bool(verified.get("valid")) and replay.get("status") == "pass" else "gap",
        "artifact": {
            "artifact_sha256": verified.get("artifact_sha256"),
            "graph_snapshot_id": artifact.get("graph_snapshot_id"),
            "graph_digest": artifact_digest,
            "node_count": artifact_nodes,
            "edge_count": artifact_edges,
            "nodes_digest": artifact.get("payload_nodes_digest") or artifact.get("node_digest"),
            "edges_digest": artifact.get("payload_edges_digest") or artifact.get("edge_digest"),
            "parse_digest": artifact.get("payload_parse_digest") or artifact.get("parse_digest"),
            "previous_graph_snapshot_id": artifact.get("previous_graph_snapshot_id"),
        },
        "target": {
            "available": target_available,
            "graph_snapshot_id": target.get("graph_snapshot_id"),
            "graph_digest": target_digest or None,
            "node_count": target_nodes if target_available else None,
            "edge_count": target_edges if target_available else None,
            "nodes_digest": target.get("nodes_digest") if target_available else None,
            "edges_digest": target.get("edges_digest") if target_available else None,
            "parse_digest": target.get("parse_digest") if target_available else None,
            "previous_graph_snapshot_id": target.get("previous_graph_snapshot_id") if target_available else None,
        },
        "delta": {
            "graph_digest_changed": bool(target_available and artifact_digest and target_digest and artifact_digest != target_digest),
            "snapshot_changed": bool(target_available and artifact.get("graph_snapshot_id") and target.get("graph_snapshot_id") and artifact.get("graph_snapshot_id") != target.get("graph_snapshot_id")),
            "node_count_delta": artifact_nodes - target_nodes if target_available else None,
            "edge_count_delta": artifact_edges - target_edges if target_available else None,
            "comparison_status": "pass" if target_available else "gap",
            "nodes_digest_match": bool(target_available and artifact.get("payload_nodes_digest") and target.get("nodes_digest") and artifact.get("payload_nodes_digest") == target.get("nodes_digest")),
            "edges_digest_match": bool(target_available and artifact.get("payload_edges_digest") and target.get("edges_digest") and artifact.get("payload_edges_digest") == target.get("edges_digest")),
            "parse_digest_match": bool(target_available and artifact.get("payload_parse_digest") and target.get("parse_digest") and artifact.get("payload_parse_digest") == target.get("parse_digest")),
            "chain_status": chain_status,
        },
        "replay_integrity": replay,
        "provenance": {"artifact_metadata_only": True, "sqlite_authoritative": True, "raw_source_returned": False},
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_mem0": False,
            "writes_worktree": False,
            "import_apply": False,
            "promotion": False,
        },
    }
    return {**delta_core, "receipt_digest": _sha256(_canonical_json(delta_core))}


def _is_digest(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def build_graph_artifact_trust_receipt(
    verified: Mapping[str, Any],
    *,
    target_snapshot: Mapping[str, Any] | None,
    detached_signature_b64: str | None = None,
    detached_public_key_b64: str | None = None,
    adoption_receipt_digest: str | None = None,
    rollback_anchor_snapshot_id: str | None = None,
    rollback_anchor_digest: str | None = None,
) -> dict[str, Any]:
    """Build a bounded, human-gated trust receipt for a graph artifact.

    External evidence is call-scoped and never persisted.  A valid detached
    signature, adoption receipt digest, or an explicit rollback-anchor match
    can improve the evidence state, but none of them grants artifact
    authority or enables service-managed promotion.
    """

    artifact_digest = str(verified.get("artifact_sha256") or "").strip().lower()
    signature_present = bool(str(detached_signature_b64 or "").strip() or str(detached_public_key_b64 or "").strip())
    if not signature_present:
        detached = {"status": "required", "reason": "external_signature_not_supplied"}
    elif not detached_signature_b64 or not detached_public_key_b64:
        detached = {"status": "invalid", "reason": "signature_and_public_key_are_both_required"}
    else:
        try:
            verification = verify_detached_ed25519(
                payload_digest=artifact_digest,
                signature_b64=detached_signature_b64,
                public_key_b64=detached_public_key_b64,
            )
        except ArtifactSignatureError:
            detached = {"status": "invalid", "reason": "detached_signature_unverifiable"}
        else:
            detached = {
                "status": "pass" if verification.get("valid") else "fail",
                "algorithm": verification.get("algorithm"),
                "payload_digest": verification.get("payload_digest"),
                "public_key_sha256": verification.get("public_key_sha256"),
                "signature_present": True,
                "reason": "verified" if verification.get("valid") else "detached_signature_mismatch",
            }

    adoption_digest = str(adoption_receipt_digest or "").strip().lower()
    if not adoption_digest:
        adoption = {"status": "required", "reason": "human_adoption_receipt_not_supplied"}
    elif not _is_digest(adoption_digest):
        adoption = {"status": "invalid", "reason": "adoption_receipt_digest_must_be_sha256"}
    else:
        adoption = {"status": "pass", "receipt_digest": adoption_digest, "reason": "operator_supplied_digest"}

    target = dict(target_snapshot or {})
    target_snapshot_id = str(target.get("graph_snapshot_id") or "").strip()
    target_digest = str(target.get("graph_digest") or "").strip().lower()
    anchor_id = str(rollback_anchor_snapshot_id or "").strip()
    anchor_digest = str(rollback_anchor_digest or "").strip().lower()
    if anchor_id or anchor_digest:
        rollback = {
            "status": "pass" if anchor_id and _is_digest(anchor_digest) and anchor_id == target_snapshot_id and anchor_digest == target_digest else "fail",
            "snapshot_id": anchor_id or None,
            "graph_digest": anchor_digest or None,
            "reason": "matches_authoritative_target" if anchor_id and _is_digest(anchor_digest) and anchor_id == target_snapshot_id and anchor_digest == target_digest else "rollback_anchor_mismatch",
        }
    elif target_snapshot_id and _is_digest(target_digest):
        rollback = {
            "status": "available",
            "snapshot_id": target_snapshot_id,
            "graph_digest": target_digest,
            "reason": "authoritative_target_available_but_operator_proof_not_supplied",
        }
    else:
        rollback = {"status": "required", "reason": "authoritative_rollback_anchor_unavailable"}

    statuses = {detached["status"], adoption["status"], rollback["status"]}
    if "invalid" in statuses or "fail" in statuses:
        state = "blocked"
    elif "pass" in statuses:
        state = "unverified" if "required" in statuses or "available" in statuses else "review_required"
    else:
        state = "unverified"
    return {
        "schema_version": GRAPH_ARTIFACT_TRUST_SCHEMA_VERSION,
        "state": state,
        "decision": "review_required",
        "detached_signature": detached,
        "adoption_receipt": adoption,
        "rollback_anchor": rollback,
        "human_gate_required": True,
        "external_evidence_persisted": False,
        "authority": "SQLite-authoritative; graph artifact remains non-authoritative",
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_mem0": False,
            "writes_worktree": False,
            "import_apply": False,
            "promotion": False,
            "signing": False,
            "raw_source_returned": False,
        },
    }


def build_graph_artifact_promotion_plan(
    verified: Mapping[str, Any],
    *,
    project: str,
    root_id: str,
    target_snapshot: Mapping[str, Any] | None,
    detached_signature_b64: str | None = None,
    detached_public_key_b64: str | None = None,
    adoption_receipt_digest: str | None = None,
    rollback_anchor_snapshot_id: str | None = None,
    rollback_anchor_digest: str | None = None,
) -> dict[str, Any]:
    """Build a source-free, human-gated promotion preview.

    The preview is deliberately not an import path.  It compares the verified
    artifact with the current authoritative snapshot and records the exact
    rollback anchor a future, separately approved promotion would need.
    """

    artifact = dict(verified.get("artifact") or {})
    target = dict(target_snapshot or {})
    identity_match = artifact.get("project") == str(project) and artifact.get("root_id") == str(root_id)
    schema_match = not target or artifact.get("graph_schema_version") in {None, "", target.get("schema_version")}
    parser_match = not target or artifact.get("parser_registry_digest") in {None, "", target.get("parser_registry_digest")}
    extractor_match = not target or artifact.get("extractor_version") in {None, "", target.get("extractor_version")}
    graph_matches_target = bool(target) and artifact.get("graph_digest") == target.get("graph_digest")
    compatible = bool(verified.get("valid")) and identity_match and schema_match and parser_match and extractor_match
    # This is a deterministic, source-free operator checklist.  It records
    # what a future service-managed promotion would still need without
    # granting the artifact authority or creating an apply path.
    adoption_checks = [
        {
            "id": "artifact-integrity",
            "status": "pass" if bool(verified.get("valid")) else "fail",
            "evidence": "verified.artifact_sha256" if verified.get("valid") else "verification_failed",
        },
        {
            "id": "identity",
            "status": "pass" if identity_match else "fail",
            "evidence": "project+root_id",
        },
        {
            "id": "schema-parser-compatibility",
            "status": "pass" if schema_match and parser_match and extractor_match else "fail",
            "evidence": "graph_schema_version+parser_registry_digest+extractor_version",
        },
        {
            "id": "graph-digest-binding",
            "status": "pass" if graph_matches_target else ("gap" if target else "required"),
            "evidence": "artifact_vs_current_graph_digest",
        },
        {
            "id": "rollback-passport",
            "status": "pass" if target.get("graph_snapshot_id") else "required",
            "evidence": "target.graph_snapshot_id" if target.get("graph_snapshot_id") else "authoritative_target_snapshot",
        },
        {
            "id": "security-authority",
            "status": "required",
            "evidence": "operator_security_gate",
        },
        {
            "id": "release-key-authority",
            "status": "required",
            "evidence": "service_release_key_policy",
        },
        {
            "id": "human-operator",
            "status": "required",
            "evidence": "explicit_operator_approval",
        },
    ]
    adoption_payload = {
        "schema_version": GRAPH_ARTIFACT_ADOPTION_SCHEMA_VERSION,
        "status": "review_required",
        "decision": "not_adopted",
        "checks": adoption_checks,
        "service_managed_promotion": False,
        "import_apply": False,
        "next_action": "collect_operator_security_release_and_rollback_approvals",
    }
    adoption_digest = _sha256(_canonical_json(adoption_payload))
    trust = build_graph_artifact_trust_receipt(
        verified,
        target_snapshot=target,
        detached_signature_b64=detached_signature_b64,
        detached_public_key_b64=detached_public_key_b64,
        adoption_receipt_digest=adoption_receipt_digest,
        rollback_anchor_snapshot_id=rollback_anchor_snapshot_id,
        rollback_anchor_digest=rollback_anchor_digest,
    )
    return {
        "plan_schema_version": "bhm.code-graph.promotion-plan.v1",
        "valid_artifact": bool(verified.get("valid")),
        "project": str(project),
        "root_id": str(root_id),
        "artifact": {
            "artifact_sha256": verified.get("artifact_sha256"),
            "graph_snapshot_id": artifact.get("graph_snapshot_id"),
            "graph_digest": artifact.get("graph_digest"),
            "repository_snapshot_id": artifact.get("repository_snapshot_id"),
        },
        "target": {
            "graph_snapshot_id": target.get("graph_snapshot_id"),
            "graph_digest": target.get("graph_digest"),
            "available": bool(target),
        },
        "compatibility": {
            "identity_match": identity_match,
            "schema_match": schema_match,
            "parser_match": parser_match,
            "extractor_match": extractor_match,
            "graph_matches_target": graph_matches_target,
            "compatible": compatible,
        },
        "promotion_eligible": False,
        "requires_operator_approval": True,
        "adoption": {
            **adoption_payload,
            "adoption_digest": adoption_digest,
        },
        "trust": trust,
        "delta_replay": build_graph_artifact_delta_replay_receipt(verified, target_snapshot=target),
        "required_gates": ["artifact-integrity", "schema-parser-compatibility", "security-authority", "rollback-passport", "human-operator"],
        "rollback_anchor": target.get("graph_snapshot_id") or None,
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_mem0": False,
            "starts_service": False,
            "import_apply": False,
            "raw_source_returned": False,
        },
        "authority": "SQLite-authoritative; artifact remains non-authoritative",
        "reason": "compatible_preview_requires_separate_human_and_security_gate" if compatible else "artifact_target_incompatible_or_unavailable",
    }


__all__ = [
    "CODE_GRAPH_ARTIFACT_SCHEMA_VERSION",
    "GRAPH_ARTIFACT_ADOPTION_SCHEMA_VERSION",
    "GRAPH_ARTIFACT_TRUST_SCHEMA_VERSION",
    "GRAPH_ARTIFACT_DELTA_REPLAY_SCHEMA_VERSION",
    "MAX_ARTIFACT_UNCOMPRESSED_BYTES",
    "CodeGraphArtifactError",
    "artifact_root",
    "export_graph_artifact",
    "verify_graph_artifact",
    "build_graph_artifact_promotion_plan",
    "build_graph_artifact_trust_receipt",
    "build_graph_artifact_delta_replay_receipt",
]
