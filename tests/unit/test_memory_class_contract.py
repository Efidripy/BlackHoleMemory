from __future__ import annotations

import copy
import hashlib

import pytest

from blackholememory import app as bhm_app
from blackholememory.domain import Memory
from blackholememory.memory_contracts import MemoryClass
from blackholememory.memory_contracts import MemoryClassSource
from blackholememory.memory_contracts import MemoryMetadata
from blackholememory.memory_contracts import MemoryEventRole
from blackholememory.typed_memory_contract import TypedMemoryContractUnavailable
from blackholememory.typed_memory_contract import classify_new_memory


def _record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "source_system": "test",
        "source_id": "mem_bhm_wl3001",
        "project": "blackholememory",
        "memory_type": "workflow",
        "content": "typed memory contract",
        "created_at": "2026-08-22T00:00:00Z",
        "updated_at": "2026-08-22T00:00:00Z",
        "metadata": {},
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize("memory_class", list(MemoryClass))
def test_memory_class_roundtrip_preserves_legacy_memory_type(memory_class: MemoryClass) -> None:
    extra = {}
    if memory_class is MemoryClass.PROCEDURAL:
        content_hash = hashlib.sha256(b"typed memory contract").hexdigest()
        extra["procedure_contract"] = {
            "procedure_version": "1",
            "inputs": [],
            "outputs": [],
            "preconditions": [],
            "steps": [{"step_id": "step-1", "instruction": "review"}],
            "postconditions": [],
            "approval_policy": {},
            "rollback_policy": {"instructions": ["restore"]},
            "memory_content_sha256": content_hash,
        }
    memory = Memory.from_record(
        _record(
            memory_class=memory_class.value,
            memory_class_source=MemoryClassSource.CALLER_EXPLICIT.value,
            memory_class_confidence=0.75,
            metadata=extra,
        )
    )

    assert memory.memory_type == "workflow"
    assert memory.memory_class is memory_class
    assert memory.memory_class_source is MemoryClassSource.CALLER_EXPLICIT
    assert memory.memory_class_confidence == 0.75
    roundtrip = Memory.from_record(memory.to_record())
    assert roundtrip.memory_type == memory.memory_type
    assert roundtrip.memory_class is memory.memory_class
    assert roundtrip.memory_class_source is memory.memory_class_source
    assert roundtrip.memory_class_confidence == memory.memory_class_confidence
    assert roundtrip.current_revision == memory.current_revision


def test_legacy_record_is_effectively_unclassified_without_metadata_mutation() -> None:
    record = _record(metadata={"domain": "infra"})
    original = copy.deepcopy(record)

    memory = Memory.from_record(record)

    assert record == original
    assert memory.memory_class is MemoryClass.UNCLASSIFIED
    assert memory.memory_class_source is MemoryClassSource.LEGACY_DEFAULT
    assert memory.metadata == {"domain": "infra"}
    assert memory.to_record()["metadata"] == {"domain": "infra", "revision_id": memory.current_revision.revision_id}


def test_invalid_memory_class_and_confidence_fail_closed() -> None:
    with pytest.raises(ValueError):
        Memory.from_record(_record(memory_class="belief"))
    with pytest.raises(ValueError):
        Memory.from_record(_record(memory_class_confidence=1.1))


def test_shared_metadata_contract_validates_classification_fields() -> None:
    metadata = MemoryMetadata(
        memory_class="procedural",
        memory_class_source="review-confirmed",
        memory_class_confidence=0.9,
    )

    assert metadata.model_dump(mode="json", exclude_none=True) == {
        "memory_class": "procedural",
        "memory_class_source": "review-confirmed",
        "memory_class_confidence": 0.9,
    }


def test_feature_flag_off_preserves_legacy_classification_and_rejects_typed_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BHM_TYPED_MEMORY_CONTRACT_ENABLED", "false")

    legacy = classify_new_memory(
        explicit_class=None,
        memory_type="knowledge",
        event_role=None,
        procedure_contract=None,
        capability_available=False,
    )
    assert legacy.memory_class is MemoryClass.UNCLASSIFIED

    with pytest.raises(TypedMemoryContractUnavailable, match="disabled"):
        classify_new_memory(
            explicit_class=MemoryClass.SEMANTIC,
            memory_type="knowledge",
            event_role=None,
            procedure_contract=None,
            capability_available=False,
        )


def test_feature_flag_on_requires_migration_before_typed_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BHM_TYPED_MEMORY_CONTRACT_ENABLED", "true")

    with pytest.raises(TypedMemoryContractUnavailable, match="migration_required"):
        classify_new_memory(
            explicit_class=MemoryClass.SEMANTIC,
            memory_type="knowledge",
            event_role=MemoryEventRole.FACT,
            procedure_contract=None,
            capability_available=False,
        )

    classified = classify_new_memory(
        explicit_class=MemoryClass.SEMANTIC,
        memory_type="knowledge",
        event_role=MemoryEventRole.FACT,
        procedure_contract=None,
        capability_available=True,
    )
    assert classified.memory_class is MemoryClass.SEMANTIC
    assert classified.source is MemoryClassSource.CALLER_EXPLICIT


def test_projection_pushdown_remains_closed_until_both_gates_are_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BHM_TYPED_MEMORY_CONTRACT_ENABLED", "true")
    monkeypatch.setenv("BHM_TYPED_MEMORY_PROJECTION_READY", "false")
    assert bhm_app._typed_projection_pushdown_ready() is False

    monkeypatch.setenv("BHM_TYPED_MEMORY_PROJECTION_READY", "true")
    monkeypatch.setattr(bhm_app, "typed_memory_capability_available", lambda _path: False)
    assert bhm_app._typed_projection_pushdown_ready() is False

    monkeypatch.setattr(bhm_app, "typed_memory_capability_available", lambda _path: True)
    assert bhm_app._typed_projection_pushdown_ready() is True
