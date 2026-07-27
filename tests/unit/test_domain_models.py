from __future__ import annotations

import pytest
from pydantic import ValidationError

from blackholememory.domain import Artifact
from blackholememory.domain import Lifecycle
from blackholememory.domain import Memory
from blackholememory.domain import MemoryLink
from blackholememory.domain import MemoryRevision
from blackholememory.domain import content_sha256
from blackholememory.domain import memory_from_record


def _memory_record(**overrides):
    record = {
        "source_system": "bhm",
        "source_id": "mem_bhm_domain_001",
        "project": "blackholememory",
        "agent_id": "workspace",
        "memory_type": "checkpoint",
        "content": "P2.1 domain contract is backward compatible.",
        "summary": None,
        "tags": ["phase2", "architecture"],
        "session_refs": {},
        "created_at": "2026-07-13T09:00:00Z",
        "updated_at": "2026-07-13T09:05:00+00:00",
        "metadata": {
            "raw_title": "P2.1 domain contract",
            "provenance": "mcp",
            "files": ["src/blackholememory/domain.py"],
            "upsert_key": "plan:p2.1",
            "lifecycle": "draft",
            "custom_extension": {"keep": True},
        },
        "custom_top_level": {"keep": "this"},
    }
    record.update(overrides)
    return record


def test_memory_record_roundtrip_preserves_extensions_and_derives_revision():
    memory = memory_from_record(_memory_record())

    assert memory.id == "mem_bhm_domain_001"
    assert memory.lifecycle is Lifecycle.ACTIVE
    assert memory.provenance.source_kind == "mcp"
    assert memory.files == ("src/blackholememory/domain.py",)
    assert memory.session_refs == ()
    assert memory.current_revision.content_sha256 == content_sha256(memory.current_revision.content)
    assert memory.current_revision.revision_id.startswith("rev_bhm_")

    record = memory.to_record()
    assert record["custom_top_level"] == {"keep": "this"}
    assert record["metadata"]["custom_extension"] == {"keep": True}
    assert record["metadata"]["upsert_key"] == "plan:p2.1"
    assert record["metadata"]["revision_id"] == memory.current_revision.revision_id
    assert record["source_id"] == memory.id
    assert record["content"] == memory.current_revision.content
    assert Memory.from_record(record).current_revision.revision_id == memory.current_revision.revision_id


def test_memory_canonical_roundtrip_is_stable():
    memory = memory_from_record(_memory_record())

    restored = Memory.from_dict(memory.to_dict())

    assert restored.to_dict() == memory.to_dict()


def test_archived_and_purged_states_map_to_storage_lifecycle():
    archived = memory_from_record(
        _memory_record(metadata={"archived_at": "2026-07-13T09:06:00Z"})
    )
    tombstoned = memory_from_record(_memory_record(lifecycle="purged"))

    assert archived.lifecycle is Lifecycle.ARCHIVED
    assert tombstoned.lifecycle is Lifecycle.TOMBSTONED
    assert tombstoned.to_record()["metadata"]["lifecycle"] == "tombstoned"


def test_revision_hash_mismatch_is_rejected():
    with pytest.raises(ValidationError, match="does not match content"):
        MemoryRevision(
            revision_id="rev_bhm_bad",
            memory_id="mem_bhm_bad",
            content="actual",
            content_sha256="0" * 64,
            created_at="2026-07-13T09:00:00Z",
        )


def test_memory_link_roundtrip_and_deterministic_fallback_id():
    link = MemoryLink.from_record(
        {
            "project": "blackholememory",
            "source_id": "mem_bhm_a",
            "target_id": "mem_bhm_b",
            "relation": "supersedes",
            "created_at": "2026-07-13T09:00:00Z",
            "updated_at": "2026-07-13T09:01:00Z",
            "metadata": {"confidence": 0.9},
        }
    )

    assert link.id == MemoryLink.from_record(
        {
            "project": "blackholememory",
            "source_id": "mem_bhm_a",
            "target_id": "mem_bhm_b",
            "relation": "supersedes",
        }
    ).id
    assert link.to_record()["metadata"] == {"confidence": 0.9}

    with pytest.raises(ValidationError, match="must differ"):
        MemoryLink(
            id="link_bhm_loop",
            project="blackholememory",
            source_id="mem_bhm_a",
            target_id="mem_bhm_a",
            relation="related",
        )


def test_artifact_wrapper_keeps_arbitrary_registry_fields():
    artifact = Artifact.from_record(
        {
            "id": "checkpoint_bhm_001",
            "project": "blackholememory",
            "memory_id": "mem_bhm_domain_001",
            "title": "P2.1",
            "done": "domain models",
            "metadata": {"owner": "workspace"},
            "created_at": "2026-07-13T09:00:00Z",
        },
        artifact_type="checkpoint",
    )

    assert artifact.artifact_type == "checkpoint"
    assert artifact.payload["done"] == "domain models"
    assert artifact.to_record()["memory_id"] == "mem_bhm_domain_001"
