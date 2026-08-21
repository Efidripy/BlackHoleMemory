from __future__ import annotations

import copy
import hashlib

import pytest

from blackholememory.portable_bundle import (
    PortableBundleError,
    build_portable_bundle,
    dry_run_import,
)


def _fixture() -> dict:
    return {
        "memories": [
            {
                "memory_id": "mem-1",
                "project": "Jmaka",
                "lifecycle": "active",
                "content": "must never enter the bundle",
                "metadata": {"pinned": True, "note": "safe fixture"},
                "current_revision_id": "rev-1",
                "updated_at": "2026-08-21T00:00:00Z",
            }
        ],
        "links": [{"source_id": "mem-1", "target_id": "mem-2", "relation": "supports", "project": "jmaka"}],
        "artifacts": [{"artifact_id": "task-1", "artifact_type": "task", "project": "jmaka", "content": "omit"}],
        "provenance": [{"source": "repo-jmaka", "project": "jmaka", "revision": "abc123"}],
    }


def _bundle() -> dict:
    return build_portable_bundle(
        _fixture(),
        project="jmaka",
        producer_revision="4bbae8b",
        source_snapshot_digest=hashlib.sha256(b"fixture").hexdigest(),
        created_at="2026-08-21T00:00:00Z",
    )


def test_bundle_is_deterministic_and_redacted() -> None:
    first = _bundle()
    second = _bundle()
    assert first == second
    rendered = repr(first)
    assert "must never enter" not in rendered
    assert "content" not in rendered
    receipt = dry_run_import(first)
    assert receipt["valid"] is True
    assert receipt["writes_sqlite"] is False
    assert receipt["writes_qdrant"] is False
    assert receipt["counts"] == {"memories": 1, "links": 1, "artifacts": 1, "provenance": 1}


def test_tampered_bundle_fails_closed() -> None:
    tampered = copy.deepcopy(_bundle())
    tampered["sections"]["memories"]["items"][0]["lifecycle"] = "deleted"
    with pytest.raises(PortableBundleError, match="digest"):
        dry_run_import(tampered)


def test_foreign_project_is_rejected_before_build() -> None:
    snapshot = _fixture()
    snapshot["links"][0]["project"] = "other"
    with pytest.raises(PortableBundleError, match="project scope"):
        build_portable_bundle(
            snapshot,
            project="jmaka",
            producer_revision="rev",
            source_snapshot_digest=hashlib.sha256(b"fixture").hexdigest(),
            created_at="2026-08-21T00:00:00Z",
        )


def test_secret_like_provenance_is_rejected() -> None:
    snapshot = _fixture()
    snapshot["provenance"][0]["revision"] = "token=super-secret-value"
    with pytest.raises(PortableBundleError, match="secret-like"):
        build_portable_bundle(
            snapshot,
            project="jmaka",
            producer_revision="rev",
            source_snapshot_digest=hashlib.sha256(b"fixture").hexdigest(),
            created_at="2026-08-21T00:00:00Z",
        )
