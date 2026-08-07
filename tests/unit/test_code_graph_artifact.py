from pathlib import Path
import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import pytest

from blackholememory.code_graph_artifact import CodeGraphArtifactError
from blackholememory.code_graph_artifact import export_graph_artifact
from blackholememory.code_graph_artifact import build_graph_artifact_promotion_plan
from blackholememory.code_graph_artifact import build_graph_artifact_delta_replay_receipt
from blackholememory.code_graph_artifact import verify_graph_artifact
from blackholememory.filesystem_boundaries import FilesystemBoundaryError


def _material() -> dict:
    return {
        "schema_version": "bhm.code-graph.v1",
        "graph_snapshot_id": "graph_demo",
        "repository_snapshot_id": "snapshot_demo",
        "graph_input_digest": "source_digest",
        "extractor_version": "extractor",
        "parser_registry_digest": "parser_digest",
        "graph_digest": "graph_digest",
        "summary": {"file_count": 1},
        "nodes": [{"node_id": "n1", "path": "src/main.py", "content": "secret source"}],
        "edges": [{"edge_id": "e1", "edge_kind": "contains", "source_node_id": "n1", "target_node_id": "n1"}],
        "parse_results": [{"path": "src/main.py", "status": "parsed", "source_text": "secret"}],
    }


def test_export_and_verify_are_source_free_and_integrity_bound(tmp_path: Path) -> None:
    exported = export_graph_artifact(_material(), runtime_dir=tmp_path, project="demo", root_id="root_demo")
    path = Path(exported["path"])
    assert path.is_file()
    verified = verify_graph_artifact(str(path), runtime_dir=tmp_path)
    assert verified["valid"] is True
    assert verified["artifact"]["node_count"] == 1
    assert verified["source_persisted"] is False
    assert verified["replay_integrity"]["schema_version"] == "bhm.code-graph.delta-replay-receipt.v1"
    assert verified["replay_integrity"]["status"] == "pass"
    assert verified["replay_integrity"]["checks"]["deterministic_gzip"] is True


def test_export_is_safe_on_deterministic_retry(tmp_path: Path) -> None:
    first = export_graph_artifact(_material(), runtime_dir=tmp_path, project="demo", root_id="root_demo")
    second = export_graph_artifact(_material(), runtime_dir=tmp_path, project="demo", root_id="root_demo")
    assert second["path"] == first["path"]
    assert Path(second["path"]).is_file()
    assert Path(second["manifest_path"]).is_file()


def test_delta_replay_receipt_binds_artifact_to_target_without_promotion(tmp_path: Path) -> None:
    exported = export_graph_artifact(_material(), runtime_dir=tmp_path, project="demo", root_id="root_demo")
    verified = verify_graph_artifact(exported["path"], runtime_dir=tmp_path)
    receipt = build_graph_artifact_delta_replay_receipt(
        verified,
        target_snapshot={
            "graph_snapshot_id": "graph_target",
            "graph_digest": "f" * 64,
            "summary": {"node_count": 2, "edge_count": 3},
        },
    )
    assert receipt["schema_version"] == "bhm.code-graph.delta-replay-receipt.v1"
    assert receipt["status"] == "pass"
    assert receipt["delta"]["graph_digest_changed"] is True
    assert receipt["delta"]["node_count_delta"] == -1
    assert receipt["delta"]["edge_count_delta"] == -2
    assert receipt["execution"]["promotion"] is False


def test_verify_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(CodeGraphArtifactError):
        verify_graph_artifact(str(tmp_path / ".." / "outside.json.gz"), runtime_dir=tmp_path)


def test_export_requires_completed_graph() -> None:
    with pytest.raises(CodeGraphArtifactError):
        export_graph_artifact({"nodes": [], "edges": []}, runtime_dir=Path("."), project="demo", root_id="root")


def test_export_rejects_reparse_runtime_boundary(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_runtime = tmp_path / "linked-runtime"
    try:
        linked_runtime.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError, match="symlink|reparse"):
        export_graph_artifact(_material(), runtime_dir=linked_runtime, project="demo", root_id="root_demo")


def test_export_rejects_hardlinked_artifact_target(tmp_path: Path) -> None:
    exported = export_graph_artifact(_material(), runtime_dir=tmp_path, project="demo", root_id="root_demo")
    artifact_path = Path(exported["path"])
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"sentinel")
    artifact_path.unlink()
    try:
        artifact_path.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError, match="hardlink"):
        export_graph_artifact(_material(), runtime_dir=tmp_path, project="demo", root_id="root_demo")
    assert outside.read_bytes() == b"sentinel"


def test_promotion_plan_is_compatible_preview_only(tmp_path: Path) -> None:
    exported = export_graph_artifact(_material(), runtime_dir=tmp_path, project="demo", root_id="root_demo")
    verified = verify_graph_artifact(exported["path"], runtime_dir=tmp_path)
    plan = build_graph_artifact_promotion_plan(
        verified,
        project="demo",
        root_id="root_demo",
        target_snapshot={
            "graph_snapshot_id": "graph_target",
            "graph_digest": "different",
            "schema_version": "bhm.code-graph.v1",
            "extractor_version": "extractor",
            "parser_registry_digest": "parser_digest",
        },
    )
    assert plan["compatibility"]["compatible"] is True
    assert plan["compatibility"]["graph_matches_target"] is False
    assert plan["promotion_eligible"] is False
    assert plan["delta_replay"]["schema_version"] == "bhm.code-graph.delta-replay-receipt.v1"
    assert plan["delta_replay"]["execution"]["promotion"] is False
    assert plan["execution"]["import_apply"] is False
    adoption = plan["adoption"]
    assert adoption["schema_version"] == "bhm.code-graph.adoption-receipt.v1"
    assert adoption["status"] == "review_required"
    assert adoption["decision"] == "not_adopted"
    assert adoption["service_managed_promotion"] is False
    assert adoption["import_apply"] is False
    checks = {item["id"]: item["status"] for item in adoption["checks"]}
    assert checks["artifact-integrity"] == "pass"
    assert checks["identity"] == "pass"
    assert checks["schema-parser-compatibility"] == "pass"
    assert checks["graph-digest-binding"] == "gap"
    assert checks["rollback-passport"] == "pass"
    assert checks["security-authority"] == "required"
    assert checks["release-key-authority"] == "required"
    assert checks["human-operator"] == "required"
    assert len(adoption["adoption_digest"]) == 64


def test_trust_receipt_is_explicitly_unverified_without_external_evidence(tmp_path: Path) -> None:
    exported = export_graph_artifact(_material(), runtime_dir=tmp_path, project="demo", root_id="root_demo")
    verified = verify_graph_artifact(exported["path"], runtime_dir=tmp_path)
    plan = build_graph_artifact_promotion_plan(
        verified,
        project="demo",
        root_id="root_demo",
        target_snapshot={
            "graph_snapshot_id": "graph_target",
            "graph_digest": "a" * 64,
            "schema_version": "bhm.code-graph.v1",
            "extractor_version": "extractor",
            "parser_registry_digest": "parser_digest",
        },
    )
    trust = plan["trust"]
    assert trust["schema_version"] == "bhm.code-graph.trust-receipt.v1"
    assert trust["state"] == "unverified"
    assert trust["decision"] == "review_required"
    assert trust["detached_signature"]["status"] == "required"
    assert trust["adoption_receipt"]["status"] == "required"
    assert trust["rollback_anchor"]["status"] == "available"
    assert trust["human_gate_required"] is True
    assert all(value is False for value in trust["execution"].values())


def test_trust_receipt_accepts_call_scoped_evidence_but_never_promotes(tmp_path: Path) -> None:
    material = _material()
    material["graph_digest"] = "b" * 64
    exported = export_graph_artifact(material, runtime_dir=tmp_path, project="demo", root_id="root_demo")
    verified = verify_graph_artifact(exported["path"], runtime_dir=tmp_path)
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    signature = private.sign(bytes.fromhex(verified["artifact_sha256"]))
    target = {
        "graph_snapshot_id": "graph_target",
        "graph_digest": "c" * 64,
        "schema_version": "bhm.code-graph.v1",
        "extractor_version": "extractor",
        "parser_registry_digest": "parser_digest",
    }
    plan = build_graph_artifact_promotion_plan(
        verified,
        project="demo",
        root_id="root_demo",
        target_snapshot=target,
        detached_signature_b64=base64.b64encode(signature).decode("ascii"),
        detached_public_key_b64=base64.b64encode(public.public_bytes_raw()).decode("ascii"),
        adoption_receipt_digest="d" * 64,
        rollback_anchor_snapshot_id="graph_target",
        rollback_anchor_digest="c" * 64,
    )
    trust = plan["trust"]
    assert trust["state"] == "review_required"
    assert trust["detached_signature"]["status"] == "pass"
    assert trust["adoption_receipt"]["status"] == "pass"
    assert trust["rollback_anchor"]["status"] == "pass"
    assert trust["human_gate_required"] is True
    assert trust["execution"]["promotion"] is False
    assert plan["promotion_eligible"] is False


def test_trust_receipt_rejects_malformed_external_evidence_without_writes(tmp_path: Path) -> None:
    exported = export_graph_artifact(_material(), runtime_dir=tmp_path, project="demo", root_id="root_demo")
    verified = verify_graph_artifact(exported["path"], runtime_dir=tmp_path)
    plan = build_graph_artifact_promotion_plan(
        verified,
        project="demo",
        root_id="root_demo",
        target_snapshot={"graph_snapshot_id": "graph_target", "graph_digest": "e" * 64},
        detached_signature_b64="not-base64",
        detached_public_key_b64="also-not-base64",
        adoption_receipt_digest="not-a-digest",
        rollback_anchor_snapshot_id="wrong",
        rollback_anchor_digest="f" * 64,
    )
    trust = plan["trust"]
    assert trust["state"] == "blocked"
    assert trust["detached_signature"]["status"] == "invalid"
    assert trust["adoption_receipt"]["status"] == "invalid"
    assert trust["rollback_anchor"]["status"] == "fail"
    assert trust["execution"]["writes_sqlite_state"] is False
