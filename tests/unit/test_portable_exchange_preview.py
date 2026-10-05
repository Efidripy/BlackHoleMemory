from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import ast

import pytest

from blackholememory.portable_bundle import build_portable_bundle
from blackholememory.portable_exchange_preview import (
    MANIFEST_SCHEMA_VERSION,
    PortableExchangePreviewError,
    build_portable_exchange_quarantine_preview,
    verify_portable_exchange_preview,
)


def _digest(value: object) -> str:
    import json

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _bundle() -> dict:
    snapshot = {
        "memories": [{"memory_id": "mem-1", "project": "blackholememory", "lifecycle": "active", "metadata": {}}],
        "links": [],
        "artifacts": [],
        "provenance": [{"source": "fixture", "project": "blackholememory"}],
    }
    return build_portable_bundle(
        snapshot,
        project="blackholememory",
        producer_revision="fixture-revision",
        source_snapshot_digest=hashlib.sha256(b"fixture").hexdigest(),
        created_at="2026-10-05T00:00:00Z",
    )


def _manifest(bundle: dict) -> dict:
    value = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_id": "manifest:fixture-1",
        "bundle_digest": bundle["bundle_digest"],
        "authority": "sqlite-authoritative",
        "project_scope": bundle["project_scope"],
        "source_snapshot_digest": bundle["source_snapshot_digest"],
        "producer_revision": bundle["producer_revision"],
        "outbox_watermark": {"cursor_digest": hashlib.sha256(b"cursor").hexdigest(), "pending_count": 0},
        "projection_generation": {
            "generation_digest": hashlib.sha256(b"generation").hexdigest(),
            "projection_included": False,
            "rebuild_required": True,
        },
        "redaction_policy_version": "bhm.redaction.v1",
        "created_at": "2026-10-05T00:00:00Z",
    }
    value["manifest_digest"] = _digest(value)
    return value


def test_preview_is_deterministic_content_free_and_no_write() -> None:
    bundle = _bundle()
    manifest = _manifest(bundle)
    first = build_portable_exchange_quarantine_preview(bundle, manifest)
    second = build_portable_exchange_quarantine_preview(bundle, manifest)

    assert first == second
    assert verify_portable_exchange_preview(first)["valid"] is True
    assert first["decision"] == "quarantine"
    assert first["apply_permitted"] is False
    assert first["rollback"]["action"] == "discard_preview"
    assert all(value is False for value in first["execution"].values())
    rendered = repr(first)
    for forbidden in ("mem-1", "fixture-revision", "content", "vector", "secret"):
        assert forbidden not in rendered


def test_replay_and_rollback_do_not_mutate_inputs() -> None:
    bundle = _bundle()
    manifest = _manifest(bundle)
    before_bundle, before_manifest = copy.deepcopy(bundle), copy.deepcopy(manifest)

    receipt = build_portable_exchange_quarantine_preview(bundle, manifest)

    assert bundle == before_bundle
    assert manifest == before_manifest
    assert receipt["rollback"] == {"action": "discard_preview", "persistent_state_created": False}


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item.__setitem__("unexpected", True), "fields"),
        (lambda item: item.__setitem__("signature", "not-allowed"), "fields"),
        (lambda item: item.__setitem__("bundle_digest", "0" * 64), "digest"),
        (lambda item: item.__setitem__("project_scope", "other"), "digest"),
        (lambda item: item.__setitem__("authority", "qdrant-authoritative"), "authority"),
        (lambda item: item["outbox_watermark"].__setitem__("pending_count", -1), "out of bounds"),
        (lambda item: item["projection_generation"].__setitem__("projection_included", True), "excluded"),
        (lambda item: item["projection_generation"].__setitem__("generation_digest", "bad"), "digest"),
        (lambda item: item.__setitem__("producer_revision", "C:/unsafe/path"), "path-bearing"),
    ],
)
def test_invalid_or_privileged_manifest_fails_closed(mutate, match: str) -> None:
    bundle = _bundle()
    manifest = _manifest(bundle)
    mutate(manifest)
    with pytest.raises(PortableExchangePreviewError, match=match):
        build_portable_exchange_quarantine_preview(bundle, manifest)


def test_manifest_and_bundle_tampering_fail_closed() -> None:
    bundle = _bundle()
    manifest = _manifest(bundle)
    manifest["created_at"] = "2026-10-06T00:00:00Z"
    with pytest.raises(PortableExchangePreviewError, match="digest"):
        build_portable_exchange_quarantine_preview(bundle, manifest)

    manifest = _manifest(bundle)
    bundle["sections"]["memories"]["items"][0]["lifecycle"] = "deleted"
    with pytest.raises(PortableExchangePreviewError, match="bundle validation"):
        build_portable_exchange_quarantine_preview(bundle, manifest)


def test_recomputed_foreign_scope_still_fails_bundle_binding() -> None:
    bundle = _bundle()
    manifest = _manifest(bundle)
    manifest["project_scope"] = "other"
    without_digest = dict(manifest)
    without_digest.pop("manifest_digest")
    manifest["manifest_digest"] = _digest(without_digest)

    with pytest.raises(PortableExchangePreviewError, match="bind"):
        build_portable_exchange_quarantine_preview(bundle, manifest)


def test_preview_receipt_tampering_fails_closed() -> None:
    receipt = build_portable_exchange_quarantine_preview(_bundle(), _manifest(_bundle()))
    receipt["execution"]["writes_sqlite"] = True
    with pytest.raises(PortableExchangePreviewError, match="execution"):
        verify_portable_exchange_preview(receipt)

    receipt = build_portable_exchange_quarantine_preview(_bundle(), _manifest(_bundle()))
    receipt["preview_id"] = "accepted-import"
    with pytest.raises(PortableExchangePreviewError, match="quarantine identity"):
        verify_portable_exchange_preview(receipt)


def test_manifest_rejects_raw_vector_and_secret_like_values() -> None:
    bundle = _bundle()
    for key, value in (("vector", [1, 2]), ("url", "https://example.invalid"), ("secret", "token=unsafe")):
        manifest = _manifest(bundle)
        manifest[key] = value
        with pytest.raises(PortableExchangePreviewError):
            build_portable_exchange_quarantine_preview(bundle, manifest)


def test_module_has_no_storage_or_execution_dependencies() -> None:
    module = importlib.import_module("blackholememory.portable_exchange_preview")
    tree = ast.parse(inspect.getsource(module))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = {"sqlite3", "qdrant", "mem0", "requests", "httpx", "subprocess", "socket", "pathlib"}
    assert not imported & forbidden
